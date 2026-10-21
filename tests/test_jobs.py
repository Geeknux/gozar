"""M4: background report jobs — submit, worker, status/result/export, ownership."""

import datetime as dt
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from bi_admin.execution import jobs
from bi_admin.models import ReportJob, ReportLog
from bi_admin.semantic import semantic_site

pytestmark = pytest.mark.django_db

API = "/gozar/api/reports"
SPEC = {"semantic_model": "order_metrics", "measures": ["orders", "revenue"]}
BY_CITY = {**SPEC, "dimensions": ["city"]}
IMMEDIATE = "bi_admin.execution.jobs.ImmediateBackend"


@pytest.fixture
def gozar(settings):
    def set(**values):
        settings.GOZAR = {**settings.GOZAR, **values}

    return set


def worker(**options):
    out = StringIO()
    call_command("gozar_worker", burst=True, stdout=out, **options)
    return out.getvalue()


def submit(client, report_api, spec=BY_CITY, **params):
    resp = report_api(client, "run", spec, mode="async", **params)
    assert resp.status_code == 202, resp.content
    return resp.json()["job"]


def test_reports_are_synchronous_by_default(admin_client, orders, report_api):
    resp = report_api(admin_client, "run", BY_CITY)
    assert resp.status_code == 200 and "rows" in resp.json()
    assert not ReportJob.objects.exists()


def test_async_job_lifecycle_with_the_worker(admin_client, orders, report_api):
    sync = report_api(admin_client, "run", BY_CITY, cache="false").json()
    job = submit(admin_client, report_api, cache="false")
    assert job["status"] == "queued" and job["semantic_model"] == "order_metrics"
    status = admin_client.get(f"{API}/jobs/{job['id']}").json()
    assert status["status"] == "queued" and status["started_at"] is None
    pending = admin_client.get(f"{API}/jobs/{job['id']}/result")
    assert pending.status_code == 409 and pending.json()["code"] == "job_not_finished"

    out = worker()
    assert "Processed 1 job(s)." in out
    status = admin_client.get(f"{API}/jobs/{job['id']}").json()
    assert status["status"] == "done" and status["finished_at"]
    result = admin_client.get(f"{API}/jobs/{job['id']}/result").json()
    assert result["rows"] == sync["rows"] and result["columns"] == sync["columns"]
    log = ReportLog.objects.order_by("-id").first()
    assert (str(log.job_id), log.username, log.kind) == (job["id"], "root", "run")

    csv = admin_client.get(f"{API}/jobs/{job['id']}/export", {"format": "csv"})
    assert csv.status_code == 200 and csv["Content-Disposition"].endswith('.csv"')
    assert "Tehran" in csv.content.decode() or "تهران" in csv.content.decode()
    xlsx = admin_client.get(f"{API}/jobs/{job['id']}/export", {"format": "xlsx"})
    assert xlsx.status_code == 200
    export_log = ReportLog.objects.order_by("-id").first()
    assert (export_log.kind, str(export_log.job_id)) == ("export", job["id"])


def test_jobs_endpoint_and_listing(admin_client, orders, report_api):
    resp = report_api(admin_client, "jobs", SPEC)
    assert resp.status_code == 202
    jobs_list = admin_client.get(f"{API}/jobs").json()["jobs"]
    assert [j["id"] for j in jobs_list] == [resp.json()["job"]["id"]]


def test_a_cached_result_is_returned_at_once(admin_client, orders, report_api):
    report_api(admin_client, "run", BY_CITY)
    resp = report_api(admin_client, "run", BY_CITY, mode="async")
    assert resp.status_code == 200 and resp.json()["cache"]["hit"] is True
    assert not ReportJob.objects.exists()


def test_spec_and_permission_errors_come_back_at_submit(client, make_staff, orders, report_api):
    client.force_login(make_staff("shop.view_order"))
    bad = report_api(client, "run", {**SPEC, "measures": ["nope"]}, mode="async")
    assert bad.status_code == 422
    client.force_login(make_staff("shop.view_customer", username="other"))
    assert report_api(client, "run", SPEC, mode="async").status_code == 403
    assert not ReportJob.objects.exists()


def test_async_by_setting_and_per_semantic_model(
    admin_client, orders, report_api, gozar, monkeypatch
):
    gozar(REPORT_ASYNC=True)
    assert report_api(admin_client, "run", SPEC, cache="false").status_code == 202
    assert report_api(admin_client, "run", SPEC, mode="sync").status_code == 200
    sem = semantic_site.get("order_metrics")
    monkeypatch.setattr(sem, "run_async", False)
    assert report_api(admin_client, "run", SPEC, cache="false").status_code == 200
    gozar(REPORT_ASYNC=False)
    monkeypatch.setattr(sem, "run_async", True)
    assert report_api(admin_client, "run", SPEC, cache="false").status_code == 202


def test_immediate_backend_finishes_in_the_request(admin_client, orders, report_api, gozar):
    gozar(JOB_BACKEND=IMMEDIATE)
    job = submit(admin_client, report_api)
    assert job["status"] == "done"
    assert admin_client.get(f"{API}/jobs/{job['id']}/result").status_code == 200


def test_jobs_are_private_to_their_owner(admin_client, make_staff, orders, report_api):
    job = submit(admin_client, report_api)
    other = Client()
    other.force_login(make_staff("shop.view_order"))
    for path in ("", "/result", "/export"):
        resp = other.get(f"{API}/jobs/{job['id']}{path}")
        assert resp.status_code == 404 and resp.json()["code"] == "job_not_found"
    assert other.post(f"{API}/jobs/{job['id']}/cancel").status_code == 404
    assert other.get(f"{API}/jobs").json()["jobs"] == []
    assert admin_client.get(f"{API}/jobs/00000000-0000-0000-0000-000000000000").status_code == 404


def test_cancel(admin_client, orders, report_api):
    job = submit(admin_client, report_api)
    cancelled = admin_client.post(f"{API}/jobs/{job['id']}/cancel").json()
    assert cancelled["status"] == "cancelled"
    assert "Processed 0 job(s)." in worker()
    resp = admin_client.get(f"{API}/jobs/{job['id']}/result")
    assert resp.status_code == 410 and resp.json()["code"] == "job_cancelled"


def test_a_job_cancelled_while_running_stays_cancelled(admin_client, orders, report_api):
    job = ReportJob.objects.get(pk=submit(admin_client, report_api)["id"])
    claimed = jobs.claim()
    assert claimed.pk == job.pk and claimed.status == "running"
    jobs.cancel(claimed)
    finished = jobs.run_job(claimed)
    assert finished.status == "cancelled" and finished.result is None


def test_failures_keep_the_synchronous_status(client, make_staff, orders, report_api):
    user = make_staff("shop.view_order")
    client.force_login(user)
    job = submit(client, report_api)
    user.user_permissions.clear()  # lost access before the worker ran it
    worker()
    status = client.get(f"{API}/jobs/{job['id']}").json()
    assert status["status"] == "failed" and status["error_status"] == 403
    resp = client.get(f"{API}/jobs/{job['id']}/result")
    assert resp.status_code == 403 and resp.json()["code"] == "permission_denied"


def test_internal_errors_are_caught(admin_client, orders, report_api, monkeypatch):
    job = submit(admin_client, report_api)

    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("bi_admin.engine.run", boom)
    worker()
    resp = admin_client.get(f"{API}/jobs/{job['id']}/result")
    assert resp.status_code == 500 and resp.json()["code"] == "internal_error"


def test_jobs_run_in_the_submitters_language(admin_client, orders, report_api):
    admin_client.patch("/gozar/api/preferences", {"language": "fa"}, "application/json")
    job = submit(admin_client, report_api)
    admin_client.patch("/gozar/api/preferences", {"language": "en"}, "application/json")
    worker()
    result = admin_client.get(f"{API}/jobs/{job['id']}/result").json()
    assert result["columns"][0]["label"] == "شهر"


def test_claim_is_exclusive(admin_client, orders, report_api):
    job_id = submit(admin_client, report_api)["id"]
    assert jobs.claim(job_id) is not None
    assert jobs.claim(job_id) is None
    assert jobs.execute(job_id) is None


def test_worker_options_and_purge(admin_client, orders, report_api):
    for _ in range(3):
        submit(admin_client, report_api, cache="false")
    assert "Processed 2 job(s)." in worker(max_jobs=2)
    old = ReportJob.objects.filter(status="done").first()
    ReportJob.objects.filter(pk=old.pk).update(created_at=timezone.now() - dt.timedelta(days=30))
    out = worker(keep_days=7)
    assert "Purged 1 old job(s)." in out and "Processed 1 job(s)." in out
    assert not ReportJob.objects.filter(pk=old.pk).exists()


def test_custom_backends(admin_client, orders, report_api, gozar):
    gozar(JOB_BACKEND="bi_admin.execution.jobs.JobBackend")
    with pytest.raises(NotImplementedError):
        report_api(admin_client, "run", SPEC, mode="async")


# --- leases: a dead worker's job is requeued, then failed ----------------------------


def stale(job_id, seconds=3600):
    """Pretend the worker running ``job_id`` stopped heart-beating ``seconds`` ago."""
    ReportJob.objects.filter(pk=job_id).update(
        heartbeat_at=timezone.now() - dt.timedelta(seconds=seconds)
    )


def test_claim_takes_a_lease(admin_client, orders, report_api):
    job_id = submit(admin_client, report_api)["id"]
    job = jobs.claim(job_id)
    assert job.lease and job.heartbeat_at and job.attempts == 1
    assert jobs.beat(job) is True


def test_a_dead_workers_job_is_requeued_and_rerun(admin_client, orders, report_api):
    job_id = submit(admin_client, report_api, cache="false")["id"]
    dead = jobs.claim(job_id)  # this worker "dies": the job stays running
    assert jobs.recover_stale() == {"requeued": 0, "failed": 0}  # the lease is fresh
    stale(job_id)
    assert jobs.recover_stale() == {"requeued": 1, "failed": 0}
    status = admin_client.get(f"{API}/jobs/{job_id}").json()
    assert (status["status"], status["attempts"]) == ("queued", 1)
    out = worker()
    assert "Processed 1 job(s)." in out
    status = admin_client.get(f"{API}/jobs/{job_id}").json()
    assert (status["status"], status["attempts"]) == ("done", 2)
    # the presumed-dead worker comes back: its lease is gone, it can't touch the job
    assert jobs.beat(dead) is False
    revived = jobs.run_job(dead, heartbeat=False)
    assert revived.status == "done" and revived.attempts == 2


def test_the_worker_recovers_stale_jobs_itself(admin_client, orders, report_api):
    job_id = submit(admin_client, report_api)["id"]
    jobs.claim(job_id)
    stale(job_id)
    out = worker()
    assert "Stale jobs: 1 requeued, 0 failed." in out and "Processed 1 job(s)." in out
    assert ReportJob.objects.get(pk=job_id).status == "done"


def test_jobs_fail_after_the_last_attempt(admin_client, orders, report_api, gozar):
    gozar(JOB_MAX_ATTEMPTS=2)
    job_id = submit(admin_client, report_api)["id"]
    for _attempt in range(2):
        jobs.claim(job_id)
        stale(job_id)
        counts = jobs.recover_stale()
    assert counts == {"requeued": 0, "failed": 1}
    status = admin_client.get(f"{API}/jobs/{job_id}").json()
    assert (status["status"], status["attempts"], status["error"]["code"]) == (
        "failed",
        2,
        "worker_lost",
    )
    resp = admin_client.get(f"{API}/jobs/{job_id}/result")
    assert resp.status_code == 500 and resp.json()["code"] == "worker_lost"
    from bi_admin.models import Alert

    assert Alert.objects.get(source="jobs").key == "jobs:worker_lost:report"
    assert "Processed 0 job(s)." in worker()


def test_lease_length_is_configurable(admin_client, orders, report_api, gozar):
    job_id = submit(admin_client, report_api)["id"]
    jobs.claim(job_id)
    stale(job_id, seconds=30)
    assert jobs.recover_stale()["requeued"] == 0  # default lease: 60 s
    gozar(JOB_LEASE_SECONDS=10)
    assert jobs.recover_stale()["requeued"] == 1


def test_heartbeat_thread_beats_until_stopped():
    import time

    calls = []
    job = ReportJob(lease="x")
    with jobs.Heartbeat(job, 0.01, beat=lambda j: calls.append(j) or True):
        time.sleep(0.1)
    count = len(calls)
    assert count >= 3
    time.sleep(0.05)
    assert len(calls) == count  # stopped


def test_heartbeat_stops_when_the_lease_is_lost():
    import time

    calls = []
    with jobs.Heartbeat(ReportJob(lease="x"), 0.01, beat=lambda j: calls.append(j) and False):
        time.sleep(0.1)
    assert len(calls) == 1
