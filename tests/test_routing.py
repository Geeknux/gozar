"""M4: read-replica routing. Report, drill-down, export and value-lookup reads go
to GOZAR["REPORTS_DATABASE"]; CRUD reads, all writes, audit entries and jobs stay
on default. The "replica" alias is a separate test database, so whichever data a
query returns shows where it ran."""

import csv
import datetime as dt
import io

import pytest
from django.core.checks import run_checks

from bi_admin.execution.router import ReportsRouter, current, reporting, reports_database
from bi_admin.models import ReportLog
from shop.models import City, Customer, Order

pytestmark = pytest.mark.django_db(databases=["default", "replica"])

API = "/gozar/api"
SPEC = {"semantic_model": "order_metrics", "measures": ["orders", "revenue"]}


@pytest.fixture
def replica(settings):
    """Route reports to "replica" and give it different data than default."""
    settings.GOZAR = {**settings.GOZAR, "REPORTS_DATABASE": "replica"}
    db = "replica"
    city = City.objects.using(db).create(name="Yazd", name_en="Yazd", province="Yazd")
    customer = Customer.objects.using(db).create(
        name="Replica customer", city=city, created_at=dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    )
    for amount in (10, 20, 30):
        Order.objects.using(db).create(
            customer=customer,
            status="paid",
            amount=amount,
            created_at=dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
        )
    return db


def test_router_only_routes_reads_inside_reporting(settings):
    router = ReportsRouter()
    assert router.db_for_read(Order) is None and current() is None
    with reporting("replica") as alias:
        assert alias == "replica" and router.db_for_read(Order) == "replica"
        assert router.db_for_write(Order) is None
    assert router.db_for_read(Order) is None
    assert reports_database() == "default"
    settings.GOZAR = {**settings.GOZAR, "REPORTS_DATABASE": "replica"}
    assert reports_database() == "replica"


def test_lazy_reads_follow_the_router(orders, replica):
    assert Order.objects.count() == 9
    with reporting():
        assert Order.objects.count() == 3  # DATABASE_ROUTERS sends it to the replica
    assert Order.objects.count() == 9


def test_reports_read_the_replica(admin_client, orders, replica, report_api):
    body = report_api(admin_client, "run", {**SPEC, "dimensions": ["city"]}).json()
    assert body["rows"] == [{"city": "Yazd", "orders": 3, "revenue": 60}]
    log = ReportLog.objects.using("default").get()  # the audit entry is written to default
    assert log.database == "replica"
    assert not ReportLog.objects.using("replica").exists()


def test_drilldown_export_and_values_read_the_replica(admin_client, orders, replica, report_api):
    drill = report_api(admin_client, "drilldown", {"spec": SPEC, "cell": {}}).json()
    assert drill["count"] == 3
    assert {r["cells"][2] for r in drill["rows"]} == {"Yazd"}  # customer__city__name
    resp = report_api(admin_client, "export", {**SPEC, "dimensions": ["city"]}, format="csv")
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8")[1:])))
    assert rows[1:] == [["Yazd", "3", "60"]]
    values = admin_client.get(
        f"{API}/reports/values", {"semantic_model": "order_metrics", "dimension": "city"}
    ).json()
    assert [v["value"] for v in values["values"]] == ["Yazd"]


def test_crud_reads_and_writes_stay_on_default(admin_client, orders, replica):
    assert admin_client.get(f"{API}/crud/shop/order/").json()["count"] == 9
    resp = admin_client.post(
        f"{API}/crud/shop/city/",
        {"name": "Tabriz", "province": "East Azerbaijan"},
        "application/json",
    )
    assert resp.status_code == 201
    assert City.objects.using("default").filter(name="Tabriz").exists()
    assert not City.objects.using("replica").filter(name="Tabriz").exists()


def test_jobs_are_stored_on_default_and_read_the_replica(
    admin_client, orders, replica, report_api, settings
):
    from bi_admin.models import ReportJob

    settings.GOZAR = {**settings.GOZAR, "JOB_BACKEND": "bi_admin.execution.jobs.ImmediateBackend"}
    job = report_api(admin_client, "run", SPEC, mode="async").json()["job"]
    assert ReportJob.objects.using("default").filter(pk=job["id"]).exists()
    result = admin_client.get(f"{API}/reports/jobs/{job['id']}/result").json()
    assert result["rows"] == [{"orders": 3, "revenue": 60}]


def test_cache_entries_are_per_database(admin_client, orders, replica, report_api, settings):
    on_replica = report_api(admin_client, "run", SPEC).json()
    settings.GOZAR = {**settings.GOZAR, "REPORTS_DATABASE": None}
    on_default = report_api(admin_client, "run", SPEC).json()
    assert on_default["cache"]["hit"] is False
    assert (on_replica["rows"][0]["orders"], on_default["rows"][0]["orders"]) == (3, 9)


def check_ids():
    return {m.id for m in run_checks(tags=["database"], databases=[])}


def test_system_checks(settings):
    settings.GOZAR = {**settings.GOZAR, "REPORTS_DATABASE": "nope"}
    assert "bi_admin.E002" in check_ids()
    settings.GOZAR = {**settings.GOZAR, "REPORTS_DATABASE": "replica"}
    settings.DATABASE_ROUTERS = []
    assert "bi_admin.W001" in check_ids()
    settings.DATABASE_ROUTERS = ["bi_admin.execution.router.ReportsRouter"]
    assert not {"bi_admin.W001", "bi_admin.E002"} & check_ids()
