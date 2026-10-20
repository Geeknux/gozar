"""M6.5 Part C: the System page — permissions, database statistics per backend,
exact counts, caching, checks, migrations, job queue, usage and media."""

import datetime as dt
import json

import pytest
from django.conf import settings as django_settings
from django.core.files.base import ContentFile
from django.db import DatabaseError, connection, connections
from django.utils import timezone

from bi_admin.models import ChangeLog, ReportJob, ReportLog, ScheduleDelivery
from bi_admin.system import db, info
from shop.models import City, Order, Product

API = "/gozar/api/system"

pytestmark = pytest.mark.django_db


@pytest.fixture
def sysadmin(client, make_staff):
    client.force_login(make_staff("bi_admin.view_system", username="ops"))
    return client


def get(client, path, **params):
    resp = client.get(f"{API}/{path}", params)
    assert resp.status_code == 200, resp.content
    return resp.json()


def tables_by_name(data):
    return {t["name"]: t for t in data["tables"]}


# --- access ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["overview", "database", "checks", "database/count?table=shop_order"]
)
def test_anonymous_and_unprivileged_users_are_refused(client, make_staff, path):
    assert client.get(f"{API}/{path}").status_code == 401
    client.force_login(make_staff("shop.view_order"))
    resp = client.get(f"{API}/{path}")
    assert resp.status_code == 403
    assert resp.json()["code"] == "permission_denied"


@pytest.mark.parametrize("path", ["overview", "database", "checks"])
def test_permission_or_superuser_grants_access(sysadmin, admin_client, path):
    assert sysadmin.get(f"{API}/{path}").status_code == 200
    assert admin_client.get(f"{API}/{path}").status_code == 200


def test_inactive_or_non_staff_user_with_the_permission_is_refused(client, make_staff):
    user = make_staff("bi_admin.view_system")
    user.is_staff = False
    user.save()
    client.force_login(user)
    assert client.get(f"{API}/overview").status_code == 401  # the API needs staff


def test_me_reports_the_system_flag(client, make_staff, admin_client):
    me = admin_client.get("/gozar/api/auth/me").json()
    assert me["user"]["permissions"]["system"] is True
    client.force_login(make_staff("bi_admin.view_system", username="ops"))
    assert client.get("/gozar/api/auth/me").json()["user"]["permissions"]["system"] is True
    client.force_login(make_staff(username="plain"))
    assert client.get("/gozar/api/auth/me").json()["user"]["permissions"]["system"] is False


def test_no_credentials_in_any_payload(sysadmin, seeded, monkeypatch):
    # Distinctive stand-ins (the real test password may equal the database name);
    # the connection is already open, so they are never used to connect.
    connection.ensure_connection()
    conf = django_settings.DATABASES["default"]
    monkeypatch.setitem(conf, "PASSWORD", "pw-sentinel-8c1f")
    monkeypatch.setitem(conf, "USER", "user-sentinel-77ad")
    monkeypatch.setitem(conf, "OPTIONS", {**conf.get("OPTIONS", {}), "sslkey": "opt-sentinel-3b"})
    bodies = [
        sysadmin.get(f"{API}/overview").content.decode(),
        sysadmin.get(f"{API}/database").content.decode(),
        sysadmin.get(f"{API}/checks").content.decode(),
    ]
    secrets = [
        django_settings.SECRET_KEY,
        "pw-sentinel-8c1f",
        "user-sentinel-77ad",
        "opt-sentinel-3b",
    ]
    for body in bodies:
        for secret in secrets:
            assert secret not in body
        data = json.loads(body)
        summary = data.get("database", data)
        for key in ("PASSWORD", "USER", "OPTIONS", "password", "user", "options"):
            assert key not in summary
    summary = json.loads(bodies[0])["database"]
    assert set(summary) >= {"alias", "vendor", "engine", "version", "name", "host", "port"}
    if connection.vendor == "sqlite":
        assert "/" not in summary["name"]  # the file name, not the server path


# --- database statistics ---------------------------------------------------------------


def test_tables_are_mapped_to_models_and_classified(sysadmin, seeded):
    data = get(sysadmin, "database")
    tables = tables_by_name(data)
    assert data["table_count"] == len(data["tables"]) > 10
    order = tables["shop_order"]
    assert (order["model"], order["app"], order["kind"]) == ("shop.order", "shop", "model")
    assert order["verbose_name"] == "order"
    assert tables["shop_product_tags"]["kind"] == "m2m"
    assert tables["auth_user"]["kind"] == "django"
    assert tables["auth_user"]["model"] == "auth.user"
    assert tables["django_migrations"] == {**tables["django_migrations"], "kind": "django"}
    assert tables["django_migrations"]["model"] is None
    assert "bi_admin_systemdashboard" not in tables  # unmanaged: no table
    for t in data["tables"]:
        for key in ("table_size", "index_size", "total_size", "estimated_rows"):
            assert t[key] is None or (isinstance(t[key], int) and t[key] >= 0)
    names = {i["name"] for i in order["indexes"]}
    assert names and all(isinstance(i["columns"], list) for i in order["indexes"])
    assert any("created_at" in i["columns"] for i in order["indexes"])


def test_unmanaged_and_foreign_tables_are_marked(sysadmin):
    known = db.table_models()
    assert known["bi_admin_systemdashboard"]["kind"] == "unmanaged"
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE gozar_legacy_extra (id integer)")
    tables = tables_by_name(get(sysadmin, "database", refresh="true"))
    extra = tables["gozar_legacy_extra"]
    assert extra["kind"] == "other" and extra["model"] is None


def test_exact_count_matches_the_orm(sysadmin, seeded):
    resp = get(sysadmin, "database/count", table="shop_order")
    assert resp["rows"] == Order.objects.count() == 60
    assert resp["alias"] == "default" and resp["duration_ms"] >= 0


@pytest.mark.parametrize(
    "table",
    ["nope", "shop_order; DROP TABLE shop_city", 'shop_order" --', "SHOP_ORDER ", ""],
)
def test_exact_count_rejects_unknown_tables(sysadmin, seeded, table):
    resp = sysadmin.get(f"{API}/database/count", {"table": table})
    assert resp.status_code in (404, 422)
    if resp.status_code == 404:
        assert resp.json()["code"] == "unknown_table"
    assert City.objects.exists()  # nothing was dropped


def test_exact_count_runs_under_the_statement_timeout(sysadmin, seeded, monkeypatch):
    import bi_admin.execution.limits as limits

    used = []
    real = limits.statement_timeout

    def spy(alias, ms=None):
        used.append(alias)
        return real(alias, ms)

    monkeypatch.setattr(limits, "statement_timeout", spy)
    get(sysadmin, "database/count", table="shop_city")
    assert used == ["default"]


@pytest.mark.skipif(connection.vendor != "sqlite", reason="SQLite statistics")
def test_sqlite_dbstat_sizes(sysadmin, seeded):
    with connection.cursor() as cursor:
        try:
            db.sqlite_dbstat(cursor)
        except DatabaseError:
            pytest.skip("this SQLite build has no dbstat")
    data = get(sysadmin, "database")
    assert data["size_method"] == "dbstat"
    assert data["total_size"] > 0
    order = tables_by_name(data)["shop_order"]
    assert order["table_size"] > 0 and order["index_size"] > 0
    assert order["total_size"] == order["table_size"] + order["index_size"]
    assert all(i["size"] is None or i["size"] > 0 for i in order["indexes"])
    pk_tables = tables_by_name(data)["shop_city"]["indexes"]
    assert any(i["unique"] for i in pk_tables)  # name is unique


@pytest.mark.skipif(connection.vendor != "sqlite", reason="SQLite statistics")
def test_sqlite_fallback_without_dbstat(sysadmin, seeded, monkeypatch):
    def no_dbstat(cursor):
        raise DatabaseError("no such table: dbstat")

    monkeypatch.setattr(db, "sqlite_dbstat", no_dbstat)
    data = get(sysadmin, "database", refresh="true")
    assert data["size_method"] == "page_count"
    assert data["total_size"] > 0  # page_count × page_size
    order = tables_by_name(data)["shop_order"]
    assert order["table_size"] is None and order["total_size"] is None
    assert order["indexes"]  # still listed, without sizes
    assert all(i["size"] is None for i in order["indexes"])
    assert data["notes"] and "dbstat" in data["notes"][0]


@pytest.mark.skipif(connection.vendor != "sqlite", reason="SQLite statistics")
def test_sqlite_estimates_from_sqlite_stat1(sysadmin, seeded):
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE")
    order = tables_by_name(get(sysadmin, "database", refresh="true"))["shop_order"]
    assert order["estimated_rows"] == Order.objects.count()


@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL statistics")
def test_postgres_statistics(sysadmin, seeded):
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE shop_order")
    data = get(sysadmin, "database", refresh="true")
    assert data["size_method"] == "postgresql"
    assert data["total_size"] > 0
    order = tables_by_name(data)["shop_order"]
    assert order["table_size"] > 0 and order["index_size"] > 0
    assert order["total_size"] >= order["table_size"] + order["index_size"]
    assert order["estimated_rows"] is not None and order["estimated_rows"] >= 0
    assert order["schema"] == "public"
    primary = [i for i in order["indexes"] if i["primary"]]
    assert len(primary) == 1 and primary[0]["columns"] == ["id"] and primary[0]["unique"]
    assert all(i["size"] > 0 for i in order["indexes"])
    summary = get(sysadmin, "overview")["database"]
    assert summary["vendor"] == "postgresql" and summary["version"]
    assert summary["host"] and summary["port"]


def test_other_backends_degrade(monkeypatch, seeded):
    monkeypatch.setattr(connections["default"], "vendor", "fakedb")
    data = db.database_stats("default")
    assert data["size_method"] is None and data["total_size"] is None
    assert data["version"] is None
    order = next(t for t in data["tables"] if t["name"] == "shop_order")
    assert order["total_size"] is None and order["estimated_rows"] is None
    assert order["model"] == "shop.order"
    assert any(i["primary"] for i in order["indexes"])  # from introspection
    assert data["notes"]


@pytest.mark.django_db(databases=["default", "replica"])
def test_statistics_use_the_reports_database(client, make_staff, settings):
    settings.GOZAR = {**settings.GOZAR, "REPORTS_DATABASE": "replica"}
    City.objects.using("replica").create(name="Yazd", name_en="Yazd", province="Yazd")
    City.objects.using("replica").create(name="Bam", name_en="Bam", province="Kerman")
    client.force_login(make_staff("bi_admin.view_system", username="ops"))
    count = get(client, "database/count", table="shop_city")
    assert (count["alias"], count["rows"]) == ("replica", 2)
    assert City.objects.count() == 0  # default is untouched
    overview = get(client, "overview")
    assert overview["database"]["alias"] == "replica"
    assert [m["alias"] for m in overview["migrations"]] == ["default", "replica"]


# --- caching ---------------------------------------------------------------------------


def test_expensive_sections_are_cached_briefly(sysadmin, seeded):
    first = get(sysadmin, "database")
    assert first["cached"] is False
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE gozar_after_cache (id integer)")
    second = get(sysadmin, "database")
    assert second["cached"] is True and second["generated_at"] == first["generated_at"]
    assert "gozar_after_cache" not in tables_by_name(second)
    fresh = get(sysadmin, "database", refresh="true")
    assert fresh["cached"] is False and "gozar_after_cache" in tables_by_name(fresh)
    assert get(sysadmin, "overview")["cached"] is False
    assert get(sysadmin, "overview")["cached"] is True
    assert get(sysadmin, "checks")["cached"] is False
    assert get(sysadmin, "checks")["cached"] is True


def test_cache_timeout_zero_disables_caching(sysadmin, settings):
    settings.GOZAR = {**settings.GOZAR, "SYSTEM_CACHE_TIMEOUT": 0}
    get(sysadmin, "database")
    assert get(sysadmin, "database")["cached"] is False


def test_live_parts_of_the_overview_are_never_cached(sysadmin, admin_user):
    get(sysadmin, "overview")
    ReportJob.objects.create(user=admin_user, semantic_model="order_metrics", spec={})
    again = get(sysadmin, "overview")
    assert again["cached"] is True
    assert again["jobs"]["queued"] == 1
    assert again["cache_check"]["ok"] is True


# --- versions, checks, migrations -----------------------------------------------------


def test_versions_apps_and_cache_backends(sysadmin):
    import django

    import bi_admin

    data = get(sysadmin, "overview")
    assert data["versions"]["django"] == django.get_version()
    assert data["versions"]["gozar"] == bi_admin.__version__
    assert {"bi_admin", "shop", "django.contrib.auth"} <= {a["name"] for a in data["apps"]}
    backends = {c["alias"]: c for c in data["caches"]}
    assert backends["default"]["reports"] is True
    assert set(backends["default"]) == {"alias", "backend", "reports"}  # no LOCATION
    check = data["cache_check"]
    assert check["ok"] is True and check["error"] is None and check["duration_ms"] >= 0


def test_cache_check_reports_a_broken_cache(monkeypatch):
    class Broken:
        def set(self, *a, **k):
            raise ConnectionError("down")

    monkeypatch.setattr(info, "caches", {"default": Broken()})
    out = info.cache_check()
    assert out == {**out, "ok": False, "error": "ConnectionError"}


def test_no_unapplied_migrations_in_tests(sysadmin):
    migrations = get(sysadmin, "overview")["migrations"]
    assert migrations[0]["alias"] == "default"
    assert migrations[0]["unapplied"] == [] and migrations[0]["conflicts"] == []
    assert migrations[0]["applied"] > 10


def test_unapplied_migrations_are_listed(monkeypatch):
    from django.db.migrations.executor import MigrationExecutor

    real = MigrationExecutor.migration_plan

    def plan(self, targets, clean_start=False):
        node = self.loader.graph.nodes[("shop", "0001_initial")]
        return [*real(self, targets, clean_start), (node, False)]

    monkeypatch.setattr(MigrationExecutor, "migration_plan", plan)
    assert info.migrations()[0]["unapplied"] == [{"app": "shop", "name": "0001_initial"}]


@pytest.mark.django_db(databases=["default", "replica"])
def test_system_checks_include_deployment_checks(sysadmin, settings):
    settings.GOZAR = {**settings.GOZAR, "REPORTS_DATABASE": "replica"}
    settings.DATABASE_ROUTERS = []  # → bi_admin.W001, a regular (database) check
    data = get(sysadmin, "checks")
    by_id = {c["id"]: c for c in data["checks"]}
    deploy = [c for c in data["checks"] if c["deployment"]]
    assert deploy and all(c["id"].startswith("security.") for c in deploy)
    assert by_id["security.W009"]["level_name"] == "WARNING"  # the demo's short SECRET_KEY
    assert by_id["bi_admin.W001"]["deployment"] is False
    assert by_id["bi_admin.W001"]["hint"] is None
    assert data["counts"]["WARNING"] == sum(c["level_name"] == "WARNING" for c in data["checks"])
    levels = [c["level"] for c in data["checks"]]
    assert levels == sorted(levels, reverse=True)
    assert django_settings.SECRET_KEY not in json.dumps(data)


# --- queue, users, activity, reports, deliveries, media -------------------------------


def test_job_queue_counts_and_stale_jobs(sysadmin, admin_user):
    now = timezone.now()
    S = ReportJob.Status

    def job(status, **kw):
        return ReportJob.objects.create(
            user=admin_user, semantic_model="order_metrics", spec={}, status=status, **kw
        )

    job(S.QUEUED)
    job(S.QUEUED)
    job(S.RUNNING, started_at=now, heartbeat_at=now)
    job(
        S.RUNNING,
        started_at=now - dt.timedelta(minutes=10),
        heartbeat_at=now - dt.timedelta(minutes=5),
    )
    job(S.RUNNING, started_at=now - dt.timedelta(minutes=10))  # never sent a heartbeat
    job(S.FAILED)
    job(S.DONE)
    jobs = get(sysadmin, "overview")["jobs"]
    assert (jobs["queued"], jobs["running"], jobs["stale"]) == (2, 3, 2)
    assert (jobs["failed"], jobs["done"], jobs["cancelled"]) == (1, 1, 0)
    assert dt.datetime.fromisoformat(jobs["last_heartbeat"]) == now
    assert jobs["oldest_queued"] is not None and jobs["lease_seconds"] == 60


def test_users_and_sessions(sysadmin, django_user_model):
    django_user_model.objects.create_user("inactive", is_active=False)
    users = get(sysadmin, "overview", refresh="true")["users"]
    assert users["total"] == 2 and users["active"] == 1
    assert users["staff"] == 1 and users["superusers"] == 0
    assert users["sessions"] == 1  # the logged-in test client


def test_sessions_unavailable_with_another_session_engine(settings):
    settings.SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
    out = info.users()
    assert out["sessions"] is None and out["session_engine"].endswith("signed_cookies")


def test_changelog_activity_per_day(sysadmin, admin_user):
    for action in ("add", "add", "change", "delete"):
        ChangeLog.objects.create(
            user=admin_user, model_label="shop.city", object_pk="1", object_repr="x", action=action
        )
    old = ChangeLog.objects.create(
        user=admin_user, model_label="shop.city", object_pk="1", object_repr="x", action="add"
    )
    ChangeLog.objects.filter(pk=old.pk).update(timestamp=timezone.now() - dt.timedelta(days=45))
    activity = get(sysadmin, "overview")["activity"]
    assert activity["days"] == 30 and len(activity["series"]) == 30
    assert activity["totals"] == {"add": 2, "change": 1, "delete": 1}
    today = timezone.localdate().isoformat()
    assert activity["series"][-1] == {"date": today, "add": 2, "change": 1, "delete": 1}
    assert {a["value"] for a in activity["actions"]} == {"add", "change", "delete"}


def test_report_usage_most_used_and_slowest(sysadmin):
    def log(model, kind, ms, status="ok", age_days=0):
        entry = ReportLog.objects.create(
            username="x", kind=kind, semantic_model=model, status=status, duration_ms=ms
        )
        if age_days:
            ReportLog.objects.filter(pk=entry.pk).update(
                timestamp=timezone.now() - dt.timedelta(days=age_days)
            )

    for ms in (10, 20, 30):
        log("order_metrics", "run", ms)
    log("order_metrics", "run", None, status="failed")
    log("order_metrics", "export", 900)
    log("customer_metrics", "run", 400)
    log("customer_metrics", "run", 99_999, age_days=40)  # outside the window
    reports = get(sysadmin, "overview")["reports"]
    assert (reports["total"], reports["failed"]) == (6, 1)
    top = reports["most_used"][0]
    assert (top["semantic_model"], top["kind"], top["count"], top["failed"]) == (
        "order_metrics",
        "run",
        4,
        1,
    )
    assert (top["avg_ms"], top["max_ms"]) == (20, 30)
    assert top["kind_display"] == "Run"
    slowest = [(r["semantic_model"], r["kind"], r["avg_ms"]) for r in reports["slowest"]]
    assert slowest == [
        ("order_metrics", "export", 900),
        ("customer_metrics", "run", 400),
        ("order_metrics", "run", 20),
    ]


def test_recent_deliveries(sysadmin):
    for i, status in enumerate(["sent", "failed", "skipped"]):
        ScheduleDelivery.objects.create(
            schedule_name=f"S{i}", username="manager", status=status, error="x" * 1000
        )
    rows = get(sysadmin, "overview")["deliveries"]
    assert [r["schedule"] for r in rows] == ["S2", "S1", "S0"]
    assert rows[1]["status"] == "failed" and rows[1]["status_display"] == "Failed"
    assert len(rows[0]["error"]) == 300


def test_media_usage_on_the_filesystem(sysadmin, settings):
    root = settings.MEDIA_ROOT
    (root / "products").mkdir(parents=True)
    (root / "products" / "a.bin").write_bytes(b"x" * 1000)
    (root / "b.txt").write_bytes(b"y" * 24)
    media = get(sysadmin, "overview")["media"]
    assert (media["method"], media["bytes"], media["files"]) == ("filesystem", 1024, 2)
    assert media["truncated"] is False and media["url"] == "/media/"
    assert "location" not in media


def test_media_scan_is_capped(settings, monkeypatch):
    for i in range(5):
        (settings.MEDIA_ROOT / f"{i}.txt").parent.mkdir(parents=True, exist_ok=True)
        (settings.MEDIA_ROOT / f"{i}.txt").write_bytes(b"z")
    monkeypatch.setattr(info, "MEDIA_SCAN_MAX_FILES", 3)
    media = info.media()
    assert media["files"] == 3 and media["truncated"] is True


def test_media_usage_on_other_storages(monkeypatch):
    from django.core.files.storage import InMemoryStorage

    storage = InMemoryStorage()
    name = storage.save("products/datasheets/spec.pdf", ContentFile(b"12345"))
    monkeypatch.setattr(info, "default_storage", storage)
    monkeypatch.setattr(Product._meta.get_field("datasheet"), "storage", storage)
    Product.objects.create(name="Lamp", category="home", price=1, datasheet=name)
    Product.objects.create(name="Kettle", category="home", price=1)  # no file
    media = info.media()
    assert (media["method"], media["bytes"], media["files"]) == ("referenced", 5, 1)
