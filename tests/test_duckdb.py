"""DuckDB columnar engine (M7): every spec must give exactly the ORM engine's rows."""

import datetime as dt
from decimal import Decimal

import pytest

duckdb = pytest.importorskip("duckdb")

from conftest import TODAY, tehran  # noqa: E402
from django.db.models import Q  # noqa: E402
from test_preaggregations import EQUIVALENT, SCOPED_SPECS, as_user, both, spec  # noqa: E402

from bi_admin.engine import ReportError, run  # noqa: E402
from bi_admin.models import Materialization, ReportLog  # noqa: E402
from bi_admin.scale import columnar  # noqa: E402
from shop.models import Order  # noqa: E402

M = "order_metrics"


@pytest.fixture
def engine(settings, tmp_path):
    settings.GOZAR = {
        **settings.GOZAR,
        "SEMANTIC_ENGINES": {M: "duckdb"},
        "DUCKDB": {"PATH": str(tmp_path / "gozar.duckdb")},
    }
    return columnar.get("duckdb")


@pytest.fixture
def synced(orders, calendar_table, frozen_today, engine):
    engine.sync()
    return engine


EXTRA = [
    spec(measures=["customers"]),
    spec(measures=["customers", "orders"], dimensions=["city"], time_grain="month"),
    spec(measures=["revenue", "orders"], dimensions=["customer"]),
    spec(dimensions=["customer"], sort=[{"field": "customer", "direction": "asc"}]),
    spec(dimensions=["customer"], sort=[{"field": "customer", "direction": "desc"}], limit=2),
    spec(filters=[{"field": "customer", "op": "contains", "value": "ر"}], dimensions=["customer"]),
    spec(filters=[{"field": "amount", "op": "between", "value": [100, 500]}], measures=["orders"]),
    spec(filters=[{"field": "amount", "op": "gte", "value": 300}], dimensions=["status"]),
    spec(dimensions=["amount"], measures=["orders"]),
    spec(time_grain="month", time_dimension="paid_at", calendar="jalali"),
    spec(filters=[{"field": "paid_at", "op": "is_null", "value": True}], measures=["orders"]),
    spec(filters=[{"field": "paid_at", "op": "neq", "value": "2026-09-22"}], measures=["orders"]),
    spec(measures=["refunds", "aov"], dimensions=["province"]),
    spec(measures=["aov"], dimensions=["status"], sort=[{"field": "aov", "direction": "asc"}]),
]


class TestParity:
    @pytest.mark.parametrize("s", EQUIVALENT + EXTRA, ids=range(len(EQUIVALENT + EXTRA)))
    def test_same_rows_as_orm(self, synced, s):
        both(s, engine="duckdb")

    def test_sort_follows_the_primary_databases_collation(self, orders, calendar_table,
                                                           frozen_today, engine):  # fmt: skip
        # byte order and PostgreSQL's en_US collation disagree on all of these
        from shop.models import City, Customer

        city = City.objects.create(name="Collation", name_en="Collation", province="P")
        for i, name in enumerate(["Ali Z", "Alia", "alpha", "Beta", "پ", "ب", "ت", "É", "e"]):
            c = Customer.objects.create(name=name, city=city, created_at=tehran(2025, 1, 1))
            Order.objects.create(customer=c, status="paid", amount=10 + i,
                                 created_at=tehran(2026, 10, 2))  # fmt: skip
        engine.sync()
        for direction in ("asc", "desc"):
            s = spec(dimensions=["customer"], sort=[{"field": "customer", "direction": direction}])
            orm, duck = both(s, engine="duckdb")
        # ties on the measure are broken by the group key, in the database's order too
        both(spec(measures=["orders"], dimensions=["customer"], limit=6), engine="duckdb")

    def test_engine_selected_by_setting(self, synced):
        r = run(spec(dimensions=["customer"]), today=TODAY)
        assert r.execution["engine"] == "duckdb"
        log = ReportLog.objects.latest("id")
        assert log.engine == "duckdb"

    def test_model_attribute_selects_it_too(self, synced, settings, monkeypatch):
        from bi_admin.semantic import semantic_site

        settings.GOZAR = {**settings.GOZAR, "SEMANTIC_ENGINES": {}}
        sem = semantic_site.get(M)
        assert columnar.backend_for(sem) is None
        monkeypatch.setattr(sem, "engine", "duckdb")
        assert columnar.backend_for(sem).name == "duckdb"
        assert run(spec(), today=TODAY).execution["engine"] == "duckdb"

    def test_preaggregation_wins_when_it_matches(self, synced):
        from bi_admin.scale import refresh
        from bi_admin.scale.preaggregations import get

        refresh.refresh(get("order_metrics.daily_sales"), "rebuild")
        assert run(spec(dimensions=["city"]), today=TODAY).execution["engine"] == "preaggregation"
        assert run(spec(dimensions=["customer"]), today=TODAY).execution["engine"] == "duckdb"

    def test_jalali_calendar_gap_is_an_error_on_both(self, orders, frozen_today, engine):
        from bi_admin.calendar import table

        table.populate(dt.date(2026, 1, 1), dt.date(2026, 12, 31))  # 2025 is missing
        engine.sync()
        s = spec(time_grain="month", calendar="jalali")
        with pytest.raises(ReportError) as orm:
            run(s, today=TODAY, engine="orm")
        with pytest.raises(ReportError) as duck:
            run(s, today=TODAY, engine="duckdb")
        assert orm.value.code == duck.value.code == "calendar_not_populated"
        assert orm.value.payload == duck.value.payload


class TestScope:
    @pytest.fixture
    def seeded_synced(self, seeded, frozen_today, engine):
        from bi_admin.calendar import table

        table.populate(dt.date(2023, 1, 1), dt.date(2027, 12, 31))
        engine.sync()

    @pytest.mark.parametrize("s", SCOPED_SPECS, ids=range(len(SCOPED_SPECS)))
    def test_scoped_users(self, seeded_synced, regional, s):
        orm, duck = both(s, as_user(regional()), engine="duckdb")
        assert duck.execution["engine"] == "duckdb"

    def test_scoped_customers(self, seeded_synced, regional, admin_user):
        s = spec(measures=["revenue", "customers"], dimensions=["customer"], limit=50)
        both(s, as_user(regional()), engine="duckdb")
        scoped = run(spec(measures=["orders"]), as_user(regional(username="t2")), engine="duckdb")
        everyone = run(spec(measures=["orders"]), as_user(admin_user), engine="duckdb")
        assert 0 < scoped.rows[0]["orders"] < everyone.rows[0]["orders"]

    def test_untranslatable_rules_fall_back(self, seeded_synced, regional):
        from django.db.models import F

        from bi_admin.access import rules

        def weird(user):
            return Q(customer__city__name_en=F("customer__name"))

        rules.rule("shop.order", weird)
        try:
            r = run(spec(dimensions=["customer"]), as_user(regional()))
            assert r.execution["engine"] == "orm"
            assert r.execution["considered"][-1]["code"] == "scope_expression"
        finally:
            rules.registry.remove("shop.order", weird)

    def test_queryset_rule_values_are_evaluated(self, seeded_synced, regional):
        from bi_admin.access import rules
        from shop.models import City

        def by_city(user):
            return Q(customer__city__in=City.objects.filter(name_en__in=["Tehran", "Qom"]))

        rules.rule("shop.order", by_city)
        try:
            both(spec(dimensions=["city"]), as_user(regional()), engine="duckdb")
        finally:
            rules.registry.remove("shop.order", by_city)

    def test_denying_rule(self, seeded_synced, make_staff):
        from conftest import SHOP_VIEW

        from bi_admin.access import rules

        rules.rule("shop.order", lambda user: False)
        try:
            request = as_user(make_staff(*SHOP_VIEW))
            both(spec(measures=["orders", "customers"]), request, engine="duckdb")
            both(spec(dimensions=["customer"]), request, engine="duckdb")
        finally:
            rules.registry.remove("shop.order")

    def test_custom_queryset_falls_back(self, seeded_synced, regional, monkeypatch):
        from bi_admin.semantic import semantic_site

        sem = semantic_site.get(M)
        original = type(sem).get_queryset
        monkeypatch.setattr(
            type(sem), "get_queryset", lambda self, r: original(self, r).filter(amount__gt=0)
        )
        r = run(spec(), as_user(regional()))
        assert r.execution["engine"] == "orm"
        assert r.execution["considered"][-1]["code"] == "custom_queryset"


class TestSync:
    def test_staleness(self, synced, settings, django_capture_on_commit_callbacks):
        assert run(spec(), today=TODAY).execution["engine"] == "duckdb"
        with django_capture_on_commit_callbacks(execute=True):
            Order.objects.filter(amount=300).update(amount=Decimal(301))
            from bi_admin.execution import cache

            cache.bump(Order)
        r = run(spec(), today=TODAY, use_cache=False)
        assert r.execution["engine"] == "orm"
        assert r.execution["considered"][-1]["code"] == "stale"
        settings.GOZAR = {**settings.GOZAR, "DUCKDB": {**settings.GOZAR["DUCKDB"],
                                                       "MAX_STALENESS": 600}}  # fmt: skip
        r = run(spec(), today=TODAY, use_cache=False)
        assert r.execution["engine"] == "duckdb" and r.execution["stale"]
        assert r.cache["stored_at"] is None
        synced.sync()
        r = run(spec(), today=TODAY, use_cache=False)
        assert r.execution["engine"] == "duckdb" and not r.execution["stale"]
        both(spec(dimensions=["city"]), engine="duckdb")

    def test_sync_replaces_the_file_and_records_state(self, synced, tmp_path):
        first = (tmp_path / "gozar.duckdb").stat().st_ino
        out = synced.sync()
        assert (tmp_path / "gozar.duckdb").stat().st_ino != first
        assert out["tables"]["shop_order"] == 9
        assert set(out["tables"]) >= {"shop_order", "shop_customer", "shop_city",
                                      "bi_admin_calendarday"}  # fmt: skip
        row = Materialization.objects.get(kind="columnar", name="duckdb")
        assert row.status == "ready" and row.table == "gozar.duckdb" and row.size_bytes > 0
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]
        status = synced.status()
        assert status["installed"] and status["exists"] and status["state"]["tables"]
        assert status["semantic_models"] == [M]

    def test_not_synced_falls_back(self, orders, frozen_today, engine):
        r = run(spec(), today=TODAY)
        assert r.execution["engine"] == "orm"
        assert r.execution["considered"][-1] == {
            "engine": "duckdb",
            "code": "not_synced",
            "detail": "the DuckDB file doesn't exist yet",
        }
        with pytest.raises(ReportError) as exc:
            run(spec(), today=TODAY, engine="duckdb")
        assert exc.value.code == "engine_unavailable"

    def test_text_with_quotes_newlines_and_nulls_survives(self, synced, engine):
        from shop.models import City, Customer

        city = City.objects.create(name='Qu"ote, \\N\nline', name_en="", province="")
        Customer.objects.create(name="", phone="\\N", city=city, created_at=tehran(2025, 1, 1))
        Order.objects.create(customer=Customer.objects.get(name=""), amount=7,
                             created_at=tehran(2026, 10, 2), status="paid")  # fmt: skip
        engine.sync()
        both(spec(dimensions=["city", "customer"]), engine="duckdb")

    def test_timeout(self, synced, settings, monkeypatch):
        from bi_admin.scale.columnar import duckdb as backend

        settings.GOZAR = {**settings.GOZAR, "REPORT_STATEMENT_TIMEOUT_MS": 1}
        original = backend.Report.sql

        def slow(self):
            sql, params = original(self)
            heavy = "SELECT COUNT(*) FROM range(100000000000) a"  # interrupted long before
            return f"{sql.split(' LIMIT ')[0]} LIMIT (SELECT 1 FROM ({heavy}) x) + 1", params

        monkeypatch.setattr(backend.Report, "sql", slow)
        with pytest.raises(ReportError) as exc:
            run(spec(dimensions=["city"]), today=TODAY, engine="duckdb")
        assert exc.value.code == "statement_timeout"


def test_numbers_match_postgres_rules():
    from bi_admin.scale.columnar.numbers import pg_div

    assert pg_div(Decimal(1800), Decimal(4), 0) == Decimal("450.0000000000000000")
    assert pg_div(Decimal(10), Decimal(3), 0) == Decimal("3.3333333333333333")
    assert pg_div(Decimal("-0.05"), Decimal(3), 2) == Decimal("-0.01666666666666666667")
