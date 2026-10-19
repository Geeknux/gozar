"""M4: limits — max rows, dimensions and filters, and the statement timeout."""

import pytest
from django.db import connection, connections

from bi_admin.engine import ReportError
from bi_admin.execution import limits

pytestmark = pytest.mark.django_db

SPEC = {"semantic_model": "order_metrics", "measures": ["orders"]}
# Counts to 20 million: seconds of work on any backend, so a 50 ms timeout fires.
SLOW_SQL = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 20000000) "
    "SELECT count(*) FROM c"
)


@pytest.fixture
def limited(settings):
    def set(**values):
        settings.GOZAR = {**settings.GOZAR, **values}

    return set


def test_max_filters(admin_client, orders, report_api, limited):
    limited(REPORT_MAX_FILTERS=2)
    f = {"field": "status", "op": "neq", "value": "paid"}
    ok = report_api(admin_client, "validate", {**SPEC, "filters": [f, f]}).json()
    assert ok["valid"]
    body = report_api(admin_client, "run", {**SPEC, "filters": [f, f, f]})
    assert body.status_code == 422
    (err,) = body.json()["errors"]
    assert (err["code"], err["path"], err["value"]) == ("too_many_filters", "filters", 3)
    assert err["message"] == "At most 2 filters per report"


def test_max_dimensions(admin_client, orders, report_api, limited):
    limited(REPORT_MAX_DIMENSIONS=1)
    body = report_api(admin_client, "run", {**SPEC, "dimensions": ["city", "status"]}).json()
    assert body["errors"][0]["code"] == "too_many_dimensions"
    # the time grain counts as a dimension too
    grain = {**SPEC, "dimensions": ["city"], "time_grain": "year"}
    assert report_api(admin_client, "run", grain).json()["errors"][0]["code"] == (
        "too_many_dimensions"
    )


def test_max_rows(admin_client, orders, report_api, limited):
    limited(REPORT_MAX_ROWS=2)
    over = report_api(admin_client, "run", {**SPEC, "dimensions": ["city"], "limit": 3}).json()
    assert over["errors"][0]["code"] == "limit_exceeded"
    capped = report_api(admin_client, "run", {**SPEC, "dimensions": ["city"], "limit": None})
    body = capped.json()
    assert (body["row_count"], body["truncated"]) == (2, True)


def test_statement_timeout_stops_a_slow_query():
    with pytest.raises(ReportError) as exc:
        with limits.statement_timeout("default", ms=50):
            with connection.cursor() as cursor:
                cursor.execute(SLOW_SQL)
    assert exc.value.status == 504 and exc.value.code == "statement_timeout"
    assert exc.value.payload["timeout_ms"] == 50
    # the connection is usable afterwards and the timeout is gone
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        assert cursor.fetchone() == (1,)


def test_statement_timeout_lets_fast_queries_through_and_restores_settings():
    with limits.statement_timeout("default", ms=5000):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            assert cursor.fetchone()[0] == "0"
    if connection.vendor == "sqlite":  # the progress handler is removed again
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM (SELECT 1 UNION ALL SELECT 2)")


def test_zero_disables_the_timeout(limited):
    limited(REPORT_STATEMENT_TIMEOUT_MS=0)
    assert limits.timeout_ms() == 0
    with limits.statement_timeout("default"):
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")


def test_other_errors_pass_through():
    from django.db import DatabaseError

    with pytest.raises(DatabaseError):
        with limits.statement_timeout("default", ms=5000):
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM no_such_table")


@pytest.mark.parametrize("path", ["run", "export", "drilldown"])
def test_report_paths_return_a_clear_error_on_timeout(
    admin_client, orders, report_api, monkeypatch, path
):
    if connection.vendor != "sqlite":
        pytest.skip("simulated with SQLite's progress handler; PostgreSQL is covered above")
    t = [0.0]

    def clock():
        t[0] += 100.0
        return t[0]

    monkeypatch.setattr(limits.time, "monotonic", clock)
    monkeypatch.setattr(limits, "SQLITE_CHECK_EVERY", 1)
    spec = {**SPEC, "dimensions": ["city"]}
    body = {"spec": spec, "cell": {}} if path == "drilldown" else spec
    resp = report_api(admin_client, path, body, **({"format": "csv"} if path == "export" else {}))
    assert resp.status_code == 504
    data = resp.json()
    assert data["code"] == "statement_timeout"
    assert data["detail"] == "The report took longer than 30 seconds and was stopped."
