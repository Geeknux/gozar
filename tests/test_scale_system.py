"""M7 on the System page: the Scale section, refresh/rebuild/sync jobs and their
permissions, the worker and the management commands."""

from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from bi_admin.models import Materialization, ReportJob
from bi_admin.scale import refresh
from bi_admin.scale.preaggregations import get

API = "/gozar/api/system"
IMMEDIATE = "bi_admin.execution.jobs.ImmediateBackend"
DAILY = "order_metrics.daily_sales"

pytestmark = pytest.mark.django_db


@pytest.fixture
def sysadmin(client, make_staff):
    client.force_login(make_staff("bi_admin.view_system", username="ops"))
    return client


@pytest.fixture
def immediate(settings):
    settings.GOZAR = {**settings.GOZAR, "JOB_BACKEND": IMMEDIATE}


@pytest.fixture
def data(orders, calendar_table, frozen_today):
    return orders


ENDPOINTS = [
    ("get", "scale"),
    ("post", f"scale/preaggregations/{DAILY}/refresh"),
    ("post", f"scale/preaggregations/{DAILY}/refresh?mode=rebuild"),
    ("post", "scale/columnar/duckdb/sync"),
    ("get", "scale/jobs/00000000-0000-0000-0000-000000000000"),
]


class TestPermissions:
    @pytest.mark.parametrize("method, path", ENDPOINTS)
    def test_anonymous_and_unprivileged_are_refused(self, client, make_staff, method, path):
        assert getattr(client, method)(f"{API}/{path}").status_code == 401
        client.force_login(make_staff("shop.view_order", "shop.change_order"))
        resp = getattr(client, method)(f"{API}/{path}")
        assert resp.status_code == 403
        assert resp.json()["code"] == "permission_denied"
        assert not ReportJob.objects.exists()

    def test_non_staff_with_the_permission_is_refused(self, client, make_staff):
        user = make_staff("bi_admin.view_system")
        user.is_staff = False
        user.save()
        client.force_login(user)
        assert client.post(f"{API}/scale/preaggregations/{DAILY}/refresh").status_code == 401
        assert not ReportJob.objects.exists()

    def test_permission_or_superuser(self, sysadmin, admin_client, data, immediate):
        for c in (sysadmin, admin_client):
            assert c.get(f"{API}/scale").status_code == 200
            assert c.post(f"{API}/scale/preaggregations/{DAILY}/refresh").status_code == 202

    def test_job_rechecks_access_when_it_runs(self, client, make_staff, data, settings):
        settings.GOZAR = {**settings.GOZAR, "WORKER_REFRESHES": False}
        user = make_staff("bi_admin.view_system")
        client.force_login(user)
        job_id = client.post(f"{API}/scale/preaggregations/{DAILY}/refresh").json()["job"]["id"]
        user.user_permissions.clear()
        call_command("gozar_worker", burst=True, stdout=StringIO())
        job = ReportJob.objects.get(pk=job_id)
        assert (job.status, job.error_status, job.error["code"]) == (
            "failed",
            403,
            "permission_denied",
        )
        assert not Materialization.objects.filter(name=DAILY, status="ready").exists()


class TestScaleSection:
    def test_overview_before_and_after_a_refresh(self, sysadmin, data, immediate):
        body = sysadmin.get(f"{API}/scale").json()
        items = {p["key"]: p for p in body["preaggregations"]}
        assert set(items) == {DAILY, "order_metrics.monthly_status"}
        daily = items[DAILY]
        assert daily["state"] is None and daily["due"] == "rebuild"
        assert daily["time_grain"] == "day" and "revenue" in daily["measures"]
        assert daily["scope_fields"] == ["customer__city__name_en"]
        assert body["enabled"] and body["database"] == "default"
        assert {c["name"] for c in body["columnar"]} == {"duckdb"}
        assert body["columnar"][0]["in_use"] is False

        resp = sysadmin.post(f"{API}/scale/preaggregations/{DAILY}/refresh?mode=rebuild")
        assert resp.status_code == 202
        job = resp.json()["job"]
        assert (job["kind"], job["status"], job["target"], job["mode"]) == (
            "preaggregate", "done", DAILY, "rebuild",
        )  # fmt: skip
        assert job["outcome"]["rows"] > 0 and job["user"] == "ops"
        assert sysadmin.get(f"{API}/scale/jobs/{job['id']}").json()["status"] == "done"

        from bi_admin.engine import run

        run({"semantic_model": "order_metrics", "measures": ["revenue"], "dimensions": ["city"]})
        body = sysadmin.get(f"{API}/scale").json()
        daily = {p["key"]: p for p in body["preaggregations"]}[DAILY]
        state = daily["state"]
        assert state["status"] == "ready" and state["rows"] == job["outcome"]["rows"]
        assert state["refresh_mode"] == "rebuild" and state["refreshed_at"]
        assert state["freshness"]["fresh"] and state["definition_current"]
        assert state["hits"] == 1 and state["last_hit_at"]
        assert state["table"].startswith("gozar_pa_order_metrics_daily_sales_")
        assert daily["due"] is None
        assert [j["id"] for j in body["jobs"]] == [job["id"]]

    def test_incremental_mode_and_unknown_targets(self, sysadmin, data, immediate):
        refresh.refresh(get(DAILY), "rebuild")
        job = sysadmin.post(f"{API}/scale/preaggregations/{DAILY}/refresh?mode=incremental")
        assert job.json()["job"]["outcome"]["mode"] == "incremental"
        assert sysadmin.post(f"{API}/scale/preaggregations/nope.x/refresh").status_code == 404
        assert sysadmin.post(f"{API}/scale/columnar/clickhouse/sync").status_code == 404
        assert (
            sysadmin.post(f"{API}/scale/preaggregations/{DAILY}/refresh?mode=x").status_code == 422
        )

    def test_failed_job_shows_the_error(self, sysadmin, data, immediate, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(refresh, "insert_select", boom)
        job = sysadmin.post(f"{API}/scale/preaggregations/{DAILY}/refresh").json()["job"]
        assert job["status"] == "failed" and "disk full" in job["error"]["detail"]
        state = {p["key"]: p for p in sysadmin.get(f"{API}/scale").json()["preaggregations"]}
        assert "disk full" in state[DAILY]["state"]["last_error"]

    def test_busy_job(self, sysadmin, data, immediate):
        refresh.refresh(get(DAILY), "rebuild")
        row = Materialization.objects.get(name=DAILY)
        token = refresh.acquire(row)
        job = sysadmin.post(f"{API}/scale/preaggregations/{DAILY}/refresh").json()["job"]
        assert (job["status"], job["error_status"], job["error"]["code"]) == ("failed", 409, "busy")
        refresh.release(row, token)

    def test_maintenance_jobs_are_not_report_jobs(self, sysadmin, data, immediate):
        job = sysadmin.post(f"{API}/scale/preaggregations/{DAILY}/refresh").json()["job"]
        assert sysadmin.get("/gozar/api/reports/jobs").json()["jobs"] == []
        assert sysadmin.get(f"/gozar/api/reports/jobs/{job['id']}").status_code == 404
        report = ReportJob.objects.create(
            user=ReportJob.objects.get().user, semantic_model="order_metrics", spec={}
        )
        assert sysadmin.get(f"{API}/scale/jobs/{report.pk}").status_code == 404

    def test_database_statistics_mark_summary_tables(self, sysadmin, data):
        refresh.refresh(get(DAILY), "rebuild")
        table = Materialization.objects.get(name=DAILY).table
        tables = {
            t["name"]: t for t in sysadmin.get(f"{API}/database?refresh=true").json()["tables"]
        }
        assert tables[table]["kind"] == "preaggregation"

    def test_columnar_sync_job(self, sysadmin, data, immediate, settings, tmp_path):
        pytest.importorskip("duckdb")
        settings.GOZAR = {
            **settings.GOZAR,
            "SEMANTIC_ENGINES": {"order_metrics": "duckdb"},
            "DUCKDB": {"PATH": str(tmp_path / "copy.duckdb")},
        }
        job = sysadmin.post(f"{API}/scale/columnar/duckdb/sync").json()["job"]
        assert job["status"] == "done" and job["outcome"]["tables"]["shop_order"] == 9
        duck = sysadmin.get(f"{API}/scale").json()["columnar"][0]
        assert duck["in_use"] and duck["exists"] and duck["file"] == "copy.duckdb"
        assert duck["state"]["status"] == "ready" and duck["state"]["synced_at"]
        assert duck["state"]["freshness"]["fresh"]
        assert duck["semantic_models"] == ["order_metrics"]
        assert str(tmp_path) not in str(duck)  # the file name only, never its path


class TestWorkerAndCommands:
    def test_worker_builds_what_is_due(self, data):
        out = StringIO()
        call_command("gozar_worker", burst=True, stdout=out)
        assert "Refresh order_metrics.daily_sales (rebuild)" in out.getvalue()
        assert Materialization.objects.filter(status="ready").count() == 2
        out = StringIO()
        call_command("gozar_worker", burst=True, stdout=out)
        assert "Refresh" not in out.getvalue()  # fresh now

    def test_worker_refreshes_can_be_turned_off(self, data, settings):
        settings.GOZAR = {**settings.GOZAR, "WORKER_REFRESHES": False}
        call_command("gozar_worker", burst=True, stdout=StringIO())
        assert not Materialization.objects.exists()

    def test_worker_runs_queued_refresh_jobs(self, sysadmin, data, settings):
        settings.GOZAR = {**settings.GOZAR, "WORKER_REFRESHES": False}
        job_id = sysadmin.post(f"{API}/scale/preaggregations/{DAILY}/refresh").json()["job"]["id"]
        assert ReportJob.objects.get(pk=job_id).status == "queued"
        call_command("gozar_worker", burst=True, stdout=StringIO())
        assert ReportJob.objects.get(pk=job_id).status == "done"

    def test_preaggregate_command(self, data):
        out = StringIO()
        call_command("gozar_preaggregate", stdout=out)
        assert "order_metrics.daily_sales: rebuild" in out.getvalue()
        out = StringIO()
        call_command("gozar_preaggregate", "daily_sales", incremental=True, stdout=out)
        assert "incremental" in out.getvalue()
        out = StringIO()
        call_command("gozar_preaggregate", list=True, stdout=out)
        assert "fresh" in out.getvalue() and "monthly_status: ready" in out.getvalue()
        out = StringIO()
        call_command("gozar_preaggregate", due=True, stdout=out)
        assert "0 pre-aggregation(s)" in out.getvalue()
        with pytest.raises(CommandError, match="Unknown"):
            call_command("gozar_preaggregate", "nope", stdout=StringIO())
        Materialization.objects.filter(name=DAILY).update(table="")
        out = StringIO()
        call_command("gozar_preaggregate", "monthly_status", drop_orphans=True, stdout=out)
        assert "Dropped gozar_pa_order_metrics_daily_sales_" in out.getvalue()

    def test_columnar_sync_command(self, data, settings, tmp_path):
        out = StringIO()
        call_command("gozar_columnar_sync", stdout=out)
        assert "No semantic model" in out.getvalue()
        pytest.importorskip("duckdb")
        settings.GOZAR = {
            **settings.GOZAR,
            "SEMANTIC_ENGINES": {"order_metrics": "duckdb"},
            "DUCKDB": {"PATH": str(tmp_path / "c.duckdb")},
        }
        out = StringIO()
        call_command("gozar_columnar_sync", stdout=out)
        assert "duckdb:" in out.getvalue() and "shop_order 9" in out.getvalue()
        out = StringIO()
        call_command("gozar_columnar_sync", due=True, stdout=out)
        assert out.getvalue() == ""  # fresh
