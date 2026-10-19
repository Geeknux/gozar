"""M4: audit log of report executions (bi_admin.ReportLog) and its API."""

import pytest
from django.test import Client

from bi_admin.engine import run
from bi_admin.models import ReportLog

pytestmark = pytest.mark.django_db

API = "/gozar/api"
SPEC = {"semantic_model": "order_metrics", "measures": ["orders", "revenue"]}


def last():
    return ReportLog.objects.order_by("-id").first()


def test_a_run_is_recorded(client, regional, orders, report_api):
    user = regional()
    client.force_login(user)
    body = report_api(
        client, "run", {**SPEC, "dimensions": ["city"], "relative_period": "this_year"}
    ).json()
    log = last()
    assert (log.user, log.username, log.kind, log.status) == (user, "tehran", "run", "ok")
    assert log.semantic_model == "order_metrics"
    assert log.spec["calendar"] == "gregorian"  # normalised: the resolved calendar
    assert log.spec["dimensions"] == ["city"] and log.spec["relative_period"] == "this_year"
    assert "name_en" in log.scope and "Tehran" in log.scope  # the row rule's WHERE clause
    assert log.row_count == body["row_count"]
    assert log.duration_ms is not None and log.database == "default"
    assert (log.error_code, log.cache_hit, log.job) == ("", False, None)


def test_unrestricted_users_have_an_empty_scope(admin_client, orders, report_api):
    report_api(admin_client, "run", SPEC)
    assert last().scope == ""


def test_cache_hits_are_recorded(admin_client, orders, report_api):
    report_api(admin_client, "run", SPEC)
    report_api(admin_client, "run", SPEC)
    assert [log.cache_hit for log in ReportLog.objects.order_by("id")] == [False, True]


@pytest.mark.parametrize(
    "spec, status, code",
    [
        ({**SPEC, "measures": ["nope"]}, "invalid", "unknown_measure"),
        ({**SPEC, "semantic_model": "nope"}, "invalid", "unknown_semantic_model"),
        ({**SPEC, "time_grain": "month", "calendar": "jalali"}, "failed", "calendar_not_populated"),
    ],
)
def test_failures_are_recorded(admin_client, orders, report_api, spec, status, code):
    report_api(admin_client, "run", spec)
    log = last()
    assert (log.status, log.error_code, log.row_count) == (status, code, None)
    assert log.error


def test_denied_runs_are_recorded(client, make_staff, orders, report_api):
    client.force_login(make_staff("shop.view_customer"))
    assert report_api(client, "run", SPEC).status_code == 403
    log = last()
    assert (log.status, log.error_code, log.username) == ("denied", "permission_denied", "staff")


def test_timeouts_are_recorded(admin_client, orders, report_api, monkeypatch):
    from bi_admin.execution import limits

    monkeypatch.setattr(limits.time, "monotonic", _clock())
    monkeypatch.setattr(limits, "SQLITE_CHECK_EVERY", 1)
    if limits.connections["default"].vendor != "sqlite":
        pytest.skip("simulated with SQLite's progress handler")
    resp = report_api(admin_client, "run", {**SPEC, "dimensions": ["city"]})
    assert resp.status_code == 504
    assert (last().status, last().error_code) == ("failed", "statement_timeout")


def _clock():
    t = [0.0]

    def tick():
        t[0] += 100.0  # every reading is 100 s later: the first check is past the deadline
        return t[0]

    return tick


def test_export_and_drilldown_are_recorded(admin_client, orders, report_api):
    report_api(admin_client, "export", SPEC, format="csv")
    log = last()
    assert (log.kind, log.status, log.row_count) == ("export", "ok", 1)
    report_api(admin_client, "drilldown", {"spec": SPEC, "cell": {}})
    log = last()
    assert (log.kind, log.status, log.row_count) == ("drilldown", "ok", 9)
    bad = report_api(admin_client, "drilldown", {"spec": SPEC, "cell": {"nope": 1}})
    assert bad.status_code == 422
    assert (last().kind, last().error_code) == ("drilldown", "not_a_group_key")


def test_validate_and_value_lookups_are_not_executions(admin_client, orders, report_api):
    report_api(admin_client, "validate", SPEC)
    admin_client.get(
        f"{API}/reports/values", {"semantic_model": "order_metrics", "dimension": "city"}
    )
    assert not ReportLog.objects.exists()


def test_code_without_a_request_is_recorded_anonymously(orders):
    run(SPEC)
    assert (last().user, last().username, last().status) == (None, "", "ok")


def test_auditing_can_be_turned_off(admin_client, orders, report_api, settings):
    settings.GOZAR = {**settings.GOZAR, "AUDIT_REPORTS": False}
    report_api(admin_client, "run", SPEC)
    assert not ReportLog.objects.exists()


def test_a_failing_audit_write_does_not_break_the_report(
    admin_client, orders, report_api, monkeypatch
):
    from bi_admin.access import audit

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(audit, "_create", boom)
    assert report_api(admin_client, "run", SPEC).status_code == 200


# --- API -----------------------------------------------------------------------------


def test_audit_api_needs_the_dedicated_permission(make_staff, orders):
    c = Client()
    assert c.get(f"{API}/audit/reports").status_code == 401
    c.force_login(make_staff("shop.view_order"))
    assert c.get(f"{API}/audit/reports").status_code == 403
    assert c.get(f"{API}/auth/me").json()["user"]["permissions"] == {
        "audit": False,
        "questions": False,
    }
    auditor = Client()
    auditor.force_login(make_staff("bi_admin.view_reportlog", username="auditor"))
    assert auditor.get(f"{API}/audit/reports").status_code == 200
    assert auditor.get(f"{API}/auth/me").json()["user"]["permissions"] == {
        "audit": True,
        "questions": False,
    }


def test_audit_api_lists_and_filters(admin_client, make_staff, orders, report_api):
    viewer = Client()
    viewer.force_login(make_staff("shop.view_order", username="viewer"))
    report_api(viewer, "run", SPEC)
    report_api(viewer, "run", {**SPEC, "measures": ["nope"]})
    report_api(admin_client, "export", SPEC, format="csv")

    body = admin_client.get(f"{API}/audit/reports").json()
    assert body["count"] == 3
    newest = body["entries"][0]
    assert (newest["user"], newest["kind"], newest["kind_display"]) == ("root", "export", "Export")
    assert set(newest) >= {"spec", "scope", "duration_ms", "row_count", "cache_hit", "error_code"}
    assert {s["value"] for s in body["filters"]["status"]} == {"ok", "invalid", "denied", "failed"}
    assert body["filters"]["semantic_model"] == ["order_metrics"]

    def count(**params):
        return admin_client.get(f"{API}/audit/reports", params).json()["count"]

    assert count(user="viewer") == 2
    assert count(status="invalid") == 1
    assert count(kind="export") == 1
    assert count(error_code="unknown_measure") == 1
    assert count(q="nope") == 1
    assert count(semantic_model="other") == 0
    paged = admin_client.get(f"{API}/audit/reports", {"page_size": 2, "page": 2}).json()
    assert (paged["num_pages"], len(paged["entries"])) == (2, 1)
    assert admin_client.get(f"{API}/audit/reports", {"page": 9}).status_code == 404


def test_audit_entries_are_translated(admin_client, orders, report_api):
    report_api(admin_client, "run", SPEC)
    admin_client.patch(f"{API}/preferences", {"language": "fa"}, "application/json")
    entry = admin_client.get(f"{API}/audit/reports").json()["entries"][0]
    assert entry["kind"] == "run" and entry["status"] == "ok"
    assert entry["status_display"] != "Succeeded"
