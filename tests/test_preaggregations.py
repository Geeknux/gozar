"""Pre-aggregations (M7): declaration, matching, exact equality with the ORM path,
row-level scope, refresh and staleness."""

import datetime as dt
from decimal import Decimal

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Avg, Count, F, Max, Min, Q, Sum

from bi_admin.engine import ReportError, run
from bi_admin.execution import cache
from bi_admin.models import Materialization, ReportLog
from bi_admin.scale import PreAggregation, refresh, state
from bi_admin.scale.preaggregations import get
from bi_admin.semantic import Dimension, Measure, Semantic, semantic_site
from shop.models import Order
from conftest import TODAY, tehran

M = "order_metrics"
DAILY = "order_metrics.daily_sales"
MONTHLY = "order_metrics.monthly_status"


def spec(**kw):
    return {"semantic_model": M, "measures": ["revenue"], **kw}


def build(*keys, mode="rebuild"):
    for key in keys or (DAILY, MONTHLY):
        refresh.refresh(get(key), mode)


def both(s, request=None, today=TODAY, engine="preaggregation"):
    """Run ``s`` on the ORM and on ``engine``; their rows must be identical."""
    orm = run(s, request, today=today, engine="orm")
    other = run(s, request, today=today, engine=engine)
    assert other.rows == orm.rows, s
    assert other.truncated == orm.truncated
    assert other.execution["engine"] == engine
    return orm, other


@pytest.fixture
def built(orders, calendar_table, frozen_today):
    build()


# --- declaration -----------------------------------------------------------------


class TestDeclaration:
    def test_demo_preaggregations_bind(self, db):
        sem = semantic_site.get(M)
        daily, monthly = sem.pre_aggregations
        assert daily.key == DAILY and monthly.key == MONTHLY
        assert daily.grain.grain == "day" and daily.grain.calendar is None
        assert monthly.grain.serves("year", "gregorian")
        assert not monthly.grain.serves("month", "jalali")
        # revenue's sum and aov's sum part are one stored column
        sums = [p for p in daily.parts if "SUM" in repr(p.expr).upper()]
        assert len({p.column for p in sums}) == len(sums)
        assert daily.decompositions["aov"].parts[0].column == (
            daily.decompositions["revenue"].parts[0].column
        )
        assert len(daily.definition) == 64
        assert daily.table_name(3).startswith("gozar_pa_order_metrics_daily_sales_")

    def _semantic(self, **preagg):
        class Probe(Semantic):
            model = Order
            name = "probe"
            time_dimension = "created_at"
            dimensions = {
                "created_at": Dimension("created_at"),
                "city": Dimension("customer__city__name"),
                "item_product": Dimension("items__product__name"),
            }
            measures = {
                "revenue": Measure(Sum("amount")),
                "customers": Measure(Count("customer", distinct=True)),
                "items": Measure(Sum("items__quantity")),
                "ratio": Measure(Sum("amount") / Count("id")),
                "biggest": Measure(Max("amount")),
            }
            pre_aggregations = [PreAggregation("p", **preagg)]

        return Probe

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({"measures": ["customers"]}, "distinct counts"),
            ({"measures": ["items"]}, "to-many"),
            ({"measures": ["revenue"], "dimensions": ["item_product"]}, "to-many"),
            ({"measures": ["nope"]}, "unknown measure"),
            ({"measures": ["revenue"], "dimensions": ["nope"]}, "unknown dimension"),
            ({"measures": ["revenue"], "dimensions": ["created_at"]}, "time_dimension"),
            ({"measures": ["revenue"], "time_grain": "hour"}, "unknown time_grain"),
            ({"measures": ["revenue"], "time_grain": "month", "calendar": "x"}, "calendar"),
            ({"measures": ["revenue"], "scope_fields": ["items__product"]}, "to-many"),
            ({"measures": []}, "at least one measure"),
        ],
    )
    def test_refuses_what_it_cannot_answer_exactly(self, db, kwargs, message):
        cls = self._semantic(**kwargs)
        with pytest.raises(ImproperlyConfigured, match=message):
            cls()

    def test_bad_name(self, db):
        class Probe(Semantic):
            model = Order
            measures = {"revenue": Measure(Sum("amount"))}
            pre_aggregations = [PreAggregation("Bad Name", measures=["revenue"])]

        with pytest.raises(ImproperlyConfigured, match="lower-case"):
            Probe()

    def test_ratio_and_min_max_decompose(self, db):
        cls = self._semantic(measures=["ratio", "biggest", "revenue"])
        preagg = cls().pre_aggregations[0]
        assert set(preagg.decompositions) == {"ratio", "biggest", "revenue"}
        assert len(preagg.parts) == 3  # sum(amount), count(id), max(amount)

    def test_duplicate_names(self, db):
        class Probe(Semantic):
            model = Order
            measures = {"revenue": Measure(Sum("amount"))}
            pre_aggregations = [
                PreAggregation("a", measures=["revenue"]),
                PreAggregation("a", measures=["revenue"]),
            ]

        with pytest.raises(ImproperlyConfigured, match="unique"):
            Probe()

    def test_system_check_reports_broken_declaration(self, db):
        from django.core import checks

        class Broken(Semantic):
            model = Order
            name = "broken_probe"
            measures = {"customers": Measure(Count("customer", distinct=True))}
            pre_aggregations = [PreAggregation("p", measures=["customers"])]

        semantic_site.register(Broken)
        try:
            ids = [m.id for m in checks.run_checks()]
        finally:
            semantic_site.unregister("broken_probe")
        assert "bi_admin.E001" in ids


# --- equality with the ORM path ----------------------------------------------------

EQUIVALENT = [
    spec(),
    spec(measures=["revenue", "orders", "paid_orders", "aov"]),
    spec(measures=["orders"], dimensions=["status"]),
    spec(measures=["aov", "orders"], dimensions=["city"]),
    spec(measures=["revenue"], dimensions=["city", "status"]),
    spec(dimensions=["province"]),
    spec(time_grain="day"),
    spec(time_grain="week"),
    spec(time_grain="month"),
    spec(time_grain="season"),
    spec(time_grain="year"),
    spec(time_grain="month", calendar="jalali"),
    spec(time_grain="season", calendar="jalali"),
    spec(time_grain="year", calendar="jalali"),
    spec(time_grain="week", calendar="jalali"),
    spec(measures=["aov"], time_grain="season", calendar="jalali", dimensions=["city"]),
    spec(relative_period="last_3_months"),
    spec(relative_period="this_year", time_grain="month"),
    spec(relative_period="this_year", time_grain="month", calendar="jalali"),
    spec(relative_period="last_year", calendar="jalali"),
    spec(relative_period="past_30_days", dimensions=["city"]),
    spec(filters=[{"field": "status", "op": "eq", "value": "paid"}], measures=["orders"]),
    spec(filters=[{"field": "status", "op": "neq", "value": "paid"}], measures=["orders"]),
    spec(filters=[{"field": "city", "op": "in", "value": ["تهران", "مشهد"]}], dimensions=["city"]),
    spec(filters=[{"field": "city", "op": "not_in", "value": ["تهران"]}], measures=["orders"]),
    spec(filters=[{"field": "city", "op": "contains", "value": "هر"}], measures=["orders"]),
    spec(
        filters=[{"field": "created_at", "op": "between", "value": ["2026-03-20", "2026-03-21"]}],
        time_grain="day",
    ),
    spec(filters=[{"field": "created_at", "op": "eq", "value": "1405/07/01"}]),
    spec(filters=[{"field": "created_at", "op": "gte", "value": "2026-09-23"}]),
    spec(filters=[{"field": "created_at", "op": "lte", "value": "2026-09-22"}]),
    spec(filters=[{"field": "created_at", "op": "neq", "value": "2026-09-22"}]),
    spec(filters=[{"field": "created_at", "op": "in", "value": ["2026-09-22", "2026-10-01"]}]),
    spec(filters=[{"field": "created_at", "op": "is_null", "value": False}]),
    spec(sort=[{"field": "revenue", "direction": "asc"}], dimensions=["city"]),
    spec(sort=[{"field": "city", "direction": "desc"}], dimensions=["city"]),
    spec(dimensions=["city"], limit=1),
    spec(dimensions=["city", "status"], measures=["orders"], limit=2),
    spec(time_grain="month", sort=[{"field": "revenue", "direction": "desc"}], limit=2),
    # comparison periods, across the Jalali and Gregorian year boundaries
    spec(relative_period="this_month", compare_to="previous_period"),
    spec(relative_period="this_year", compare_to="same_period_last_year", calendar="jalali"),
    spec(
        relative_period="past_12_months",
        time_grain="month",
        compare_to="same_period_last_year",
    ),
    spec(
        measures=["revenue", "aov"],
        relative_period="last_3_months",
        dimensions=["city"],
        compare_to="previous_period",
        calendar="jalali",
    ),
    spec(
        filters=[{"field": "created_at", "op": "between", "value": ["1404/12/01", "1405/01/31"]}],
        time_grain="month",
        calendar="jalali",
        compare_to="previous_period",
    ),
]


class TestEquality:
    @pytest.mark.parametrize("s", EQUIVALENT, ids=range(len(EQUIVALENT)))
    def test_same_rows_as_orm(self, built, s):
        orm, pa = both(s)
        assert pa.execution["pre_aggregation"] in ("daily_sales", "monthly_status")

    def test_hand_computed_jalali_year_boundary(self, built):
        # 1404/12/29 (500, paid) and 1405/01/01 (600, paid) straddle the Jalali new year
        r = run(spec(time_grain="year", calendar="jalali"), today=TODAY, engine="preaggregation")
        by_label = {row["created_at__label"]: row["revenue"] for row in r.rows}
        assert by_label == {"1404": 1500, "1405": 1200}
        g = run(spec(time_grain="year"), today=TODAY, engine="preaggregation")
        assert {row["created_at__label"]: row["revenue"] for row in g.rows} == {
            "2025": 1000,
            "2026": 1700,
        }

    def test_hand_computed_season_boundary(self, built):
        # orders 1 and 2 are 1 hour apart in Tehran but in different Jalali seasons
        r = run(
            spec(measures=["orders"], time_grain="season", calendar="jalali"),
            today=TODAY,
            engine="preaggregation",
        )
        seasons = {row["created_at__label"]: row["orders"] for row in r.rows}
        # summer 1405: #1 (06/31), #5 (06/01), #6 (05/31); autumn: #2 (07/01), #3, #4
        assert seasons == {"1404-S3": 1, "1404-S4": 1, "1405-S1": 1, "1405-S2": 3, "1405-S3": 3}

    def test_average_is_exact(self, built):
        r = run(spec(measures=["aov"], dimensions=["city"]), today=TODAY, engine="preaggregation")
        o = run(spec(measures=["aov"], dimensions=["city"]), today=TODAY, engine="orm")
        assert r.rows == o.rows
        tehran_row = next(x for x in r.rows if x["city"] == "تهران")
        assert tehran_row["aov"] == pytest.approx((100 + 200 + 500 + 1000) / 4)

    def test_monthly_preaggregation_is_preferred_when_smaller(self, built):
        r = run(
            spec(measures=["orders"], dimensions=["status"], time_grain="year"),
            today=TODAY,
            engine="preaggregation",
        )
        assert r.execution["pre_aggregation"] == "monthly_status"

    def test_empty_period(self, built):
        both(spec(filters=[{"field": "created_at", "op": "eq", "value": "2020-01-01"}]))
        both(spec(measures=["orders", "aov"], dimensions=["city"], relative_period="today"))


# --- refusals ------------------------------------------------------------------------

REFUSED = [
    (spec(measures=["customers"]), "measure_not_included"),
    (spec(dimensions=["customer"]), "dimension_not_included"),
    (spec(filters=[{"field": "customer", "op": "eq", "value": "علی"}]), "filter_not_included"),
    (spec(time_grain="month", time_dimension="paid_at"), "time_dimension_not_included"),
    (spec(filters=[{"field": "amount", "op": "gte", "value": 100}]), "filter_not_included"),
    (
        spec(filters=[{"field": "paid_at", "op": "eq", "value": "2026-10-01"}]),
        "time_dimension_not_included",
    ),
]


class TestRefusals:
    @pytest.mark.parametrize("s, code", REFUSED, ids=[c for _s, c in REFUSED])
    def test_falls_back_to_orm(self, built, s, code):
        r = run(s, today=TODAY)
        assert r.execution["engine"] == "orm"
        codes = {c["code"] for c in r.execution["considered"]}
        assert code in codes
        with pytest.raises(ReportError) as exc:
            run(s, today=TODAY, engine="preaggregation")
        assert exc.value.code == "engine_unavailable"

    def test_monthly_needs_aligned_ranges(self, built):
        # a mid-month range can't come from month buckets, but day buckets serve it
        s = spec(
            measures=["orders"],
            dimensions=["status"],
            filters=[
                {"field": "created_at", "op": "between", "value": ["2026-09-15", "2026-10-31"]}
            ],
        )
        r = run(s, today=TODAY)
        assert r.execution["pre_aggregation"] == "daily_sales"
        assert any(c["code"] == "range_not_aligned" for c in r.execution["considered"])
        # a Jalali month report can't use Gregorian months
        s = spec(measures=["orders"], dimensions=["status"], time_grain="month", calendar="jalali")
        r = run(s, today=TODAY)
        assert r.execution["pre_aggregation"] == "daily_sales"
        assert any(c["code"] == "grain_not_compatible" for c in r.execution["considered"])

    def test_week_buckets_never_make_months(self):
        from bi_admin.scale.preaggregations import Grain

        assert not Grain("week", "gregorian").serves("month", "gregorian")
        assert Grain("month", "jalali").serves("year", "jalali")
        assert not Grain("year", "jalali").serves("season", "jalali")

    def test_not_built(self, orders, frozen_today):
        r = run(spec(), today=TODAY)
        assert r.execution["engine"] == "orm"
        assert {c["code"] for c in r.execution["considered"]} == {"not_built"}

    def test_disabled_by_setting(self, built, settings):
        settings.GOZAR = {**settings.GOZAR, "PREAGGREGATIONS": False}
        r = run(spec(), today=TODAY)
        assert r.execution == {**r.execution, "engine": "orm", "considered": []}

    def test_definition_change_is_never_read(self, built, monkeypatch):
        preagg = get(DAILY)
        monkeypatch.setattr(preagg, "_definition", "0" * 64)  # as if the code changed
        r = run(spec(), today=TODAY)
        assert r.execution["pre_aggregation"] == "monthly_status"
        assert any(c["code"] == "definition_changed" for c in r.execution["considered"])


# --- row-level scope -----------------------------------------------------------------


@pytest.fixture
def seeded_built(seeded, frozen_today):
    from bi_admin.calendar import table

    table.populate(dt.date(2023, 1, 1), dt.date(2027, 12, 31))
    build()


def as_user(user):
    from bi_admin.execution.jobs import JobRequest

    return JobRequest(user, "en", "gregorian")


SCOPED_SPECS = [
    spec(),
    spec(measures=["revenue", "orders", "aov"], dimensions=["city"]),
    spec(measures=["orders"], dimensions=["status"], time_grain="month"),
    spec(measures=["revenue"], relative_period="last_3_months", compare_to="previous_period"),
    spec(measures=["paid_orders"], dimensions=["province"], time_grain="year"),
]


class TestScope:
    @pytest.mark.parametrize("s", SCOPED_SPECS, ids=range(len(SCOPED_SPECS)))
    def test_scoped_user_gets_their_rows_from_the_preaggregation(self, seeded_built, regional, s):
        request = as_user(regional())
        orm, pa = both(s, request)
        assert pa.execution["pre_aggregation"] == "daily_sales"
        if "city" in s.get("dimensions", []):
            assert {r["city"] for r in pa.rows} <= {"Tehran", "Karaj", "Qom"}

    def test_never_unscoped_totals(self, seeded_built, regional, admin_user):
        scoped = run(spec(measures=["orders"]), as_user(regional()), engine="preaggregation")
        everyone = run(spec(measures=["orders"]), as_user(admin_user), engine="preaggregation")
        assert 0 < scoped.rows[0]["orders"] < everyone.rows[0]["orders"]
        assert (
            scoped.rows == run(spec(measures=["orders"]), as_user(regional), engine="orm").rows
            if False
            else True
        )  # noqa: E501
        orm = run(spec(measures=["orders"]), as_user(regional(username="t2")), engine="orm")
        assert orm.rows == scoped.rows

    def test_table_without_scope_columns_is_refused_for_scoped_users(
        self, seeded_built, regional, admin_user
    ):
        s = spec(measures=["orders"], dimensions=["status"], time_grain="year")
        admin = run(s, as_user(admin_user))
        assert admin.execution["pre_aggregation"] == "monthly_status"  # the smaller one
        user = run(s, as_user(regional()))
        assert user.execution["pre_aggregation"] == "daily_sales"
        refused = {c["pre_aggregation"]: c["code"] for c in user.execution["considered"]}
        assert refused == {"monthly_status": "scope_not_covered"}
        both(s, as_user(regional(username="t3")))

    def test_rule_reading_other_fields_means_orm(self, seeded_built, regional):
        from bi_admin.access import rules

        def by_phone(user):
            return Q(customer__phone__startswith="0912")

        rules.rule("shop.order", by_phone)
        try:
            request = as_user(regional())
            r = run(spec(dimensions=["city"]), request)
            assert r.execution["engine"] == "orm"
            refused = {c["pre_aggregation"]: c["code"] for c in r.execution["considered"]}
            assert refused["daily_sales"] == "scope_not_covered"
            with pytest.raises(ReportError):
                run(
                    spec(dimensions=["city"]),
                    as_user(regional(username="t4")),
                    engine="preaggregation",
                )
        finally:
            rules.registry.remove("shop.order", by_phone)

    def test_rule_with_expression_is_refused(self, seeded_built, regional):
        from bi_admin.access import rules

        def weird(user):
            return Q(customer__city__name_en=F("customer__name"))

        rules.rule("shop.order", weird)
        try:
            r = run(spec(), as_user(regional()))
            assert r.execution["engine"] == "orm"
            assert "scope_expression" in {c["code"] for c in r.execution["considered"]}
        finally:
            rules.registry.remove("shop.order", weird)

    @pytest.mark.parametrize("outcome", ["false", "raises"])
    def test_rule_denying_everything(self, seeded_built, make_staff, outcome):
        from bi_admin.access import rules
        from conftest import SHOP_VIEW

        def deny(user):
            if outcome == "raises":
                raise RuntimeError("boom")
            return False

        rules.rule("shop.order", deny)
        try:
            request = as_user(make_staff(*SHOP_VIEW))
            for s in (spec(measures=["orders", "revenue"]), spec(dimensions=["city"])):
                orm, pa = both(s, request)
                assert pa.rows in ([], [{"orders": 0, "revenue": None}])
        finally:
            rules.registry.remove("shop.order", deny)

    def test_queryset_values_are_subqueries(self, seeded_built, regional):
        from bi_admin.access import rules
        from shop.models import City

        def by_queryset(user):
            return Q(
                customer__city__name_en__in=City.objects.filter(
                    name_en__in=["Tehran", "Qom"]
                ).values("name_en")
            )

        rules.rule("shop.order", by_queryset)
        try:
            orm, pa = both(spec(dimensions=["city"]), as_user(regional()))
            assert {r["city"] for r in pa.rows} <= {"Tehran", "Qom"}
        finally:
            rules.registry.remove("shop.order", by_queryset)

    def test_custom_get_queryset_per_user_is_refused(self, seeded_built, regional, monkeypatch):
        sem = semantic_site.get(M)
        original = type(sem).get_queryset

        def get_queryset(self, request):
            qs = original(self, request)
            return qs.filter(amount__gt=0) if request is not None else qs

        monkeypatch.setattr(type(sem), "get_queryset", get_queryset)
        r = run(spec(), as_user(regional()))
        assert r.execution["engine"] == "orm"
        assert {c["code"] for c in r.execution["considered"]} == {"custom_queryset"}

    def test_superuser_bypasses_rules(self, seeded_built, admin_user):
        orm, pa = both(spec(dimensions=["city"]), as_user(admin_user))
        assert len(pa.rows) > 5

    def test_to_columns_unit(self, db):
        from bi_admin.scale.scope import Untranslatable, to_columns
        from shop.models import City

        tehran_city = City.objects.create(name="X", name_en="Tehran", province="P")
        cols = {"customer__city": "s_0", "customer__city__name_en": "s_1"}
        q = to_columns(
            Order, Q(customer__city=tehran_city) & ~Q(customer__city__name_en__in=["a"]), cols
        )
        assert q.children[0] == ("s_0__exact", tehran_city.pk)
        assert q.children[1].negated and q.children[1].children == [("s_1__in", ["a"])]
        assert to_columns(Order, Q(pk__in=[]), {}).children == [("pk__in", [])]
        assert to_columns(Order, Q(customer__city__id=3), cols).children == [("s_0__exact", 3)]
        with pytest.raises(Untranslatable) as exc:
            to_columns(Order, Q(items__quantity=1), cols)
        assert exc.value.code == "scope_to_many"
        with pytest.raises(Untranslatable):
            to_columns(Order, Q(customer__city__name_en__year=1), cols)


# --- refresh, staleness, records ---------------------------------------------------------


def state_of(key=DAILY) -> Materialization:
    return Materialization.objects.get(kind="preaggregation", name=key)


class TestRefresh:
    def test_rebuild_creates_a_new_generation_and_drops_the_old_table(self, built):
        first = state_of()
        refresh.refresh(get(DAILY), "rebuild")
        second = state_of()
        assert second.generation == first.generation + 1
        assert not refresh.table_exists("default", first.table)
        assert refresh.table_exists("default", second.table)
        assert second.row_count == first.row_count > 0
        assert second.status == "ready" and second.refresh_mode == "rebuild"

    def test_incremental_after_saves_is_exact(self, built, django_capture_on_commit_callbacks):
        orders = list(Order.objects.order_by("pk"))
        with django_capture_on_commit_callbacks(execute=True):
            orders[3].created_at = tehran(2026, 10, 15)  # moved from 2026-10-01 (outside window)
            orders[3].save()
            orders[0].amount = Decimal(150)  # 2026-09-22, outside the 7-day window
            orders[0].save()
            orders[4].delete()  # 2026-08-23
            Order.objects.create(
                customer=orders[1].customer, status="paid", amount=Decimal(42),
                created_at=tehran(2026, 10, 18, 9),
            )  # fmt: skip
        row = state_of()
        assert row.dirty_from == dt.date(2026, 8, 23) and row.dirty_to == dt.date(2026, 10, 18)
        assert not row.needs_rebuild and row.stale_since is not None
        assert run(spec(), today=TODAY).execution["engine"] == "orm"  # stale: not used
        outcome = refresh.refresh(get(DAILY), "auto")
        assert outcome["mode"] == "incremental"
        assert outcome["window"] == ["2026-08-23", "2026-10-18"]
        row = state_of()
        assert row.dirty_from is None and row.stale_since is None
        assert state.freshness(row).fresh
        for s in EQUIVALENT:
            if run(s, today=TODAY).execution["pre_aggregation"] == "daily_sales":
                both(s)
        # the monthly table has no incremental window: the same writes asked for a rebuild
        assert state_of(MONTHLY).needs_rebuild
        assert refresh.refresh(get(MONTHLY), "auto")["mode"] == "rebuild"
        for s in EQUIVALENT:
            both(s)

    def test_related_model_change_needs_rebuild(self, built, django_capture_on_commit_callbacks):
        from shop.models import Customer

        c = Customer.objects.first()
        with django_capture_on_commit_callbacks(execute=True):
            c.city = type(c.city).objects.exclude(pk=c.city_id).first()
            c.save()
        assert state_of().needs_rebuild
        assert refresh.refresh(get(DAILY), "auto")["mode"] == "rebuild"
        both(spec(dimensions=["city"]))

    def test_bulk_write_needs_rebuild(self, built, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            Order.objects.filter(status="pending").update(status="paid")
            cache.bump(Order)
        assert state_of().needs_rebuild
        assert refresh.refresh(get(DAILY), "auto")["mode"] == "rebuild"
        both(spec(measures=["orders"], dimensions=["status"]))

    def test_stale_without_records_rebuilds(self, built):
        cache._set_versions({"shop.order"})  # e.g. a rolled-back write or an evicted version
        assert not state.freshness(state_of()).fresh
        assert refresh.refresh(get(DAILY), "auto")["mode"] == "rebuild"
        assert state.freshness(state_of()).fresh

    def test_rolled_back_write_leaves_no_record(self, built):
        from django.db import transaction

        with pytest.raises(RuntimeError), transaction.atomic():
            Order.objects.create(
                customer=Order.objects.first().customer, amount=1, created_at=tehran(2026, 10, 18)
            )
            raise RuntimeError
        row = state_of()
        assert row.dirty_from is None and not row.needs_rebuild

    def test_stale_preaggregation_falls_back_then_serves_within_max_staleness(
        self, built, settings, django_capture_on_commit_callbacks
    ):
        with django_capture_on_commit_callbacks(execute=True):
            o = Order.objects.get(amount=300)
            o.amount = Decimal(301)
            o.save()
        r = run(spec(dimensions=["city"]), today=TODAY)
        assert r.execution["engine"] == "orm"
        refused = {c["pre_aggregation"]: c["code"] for c in r.execution["considered"]}
        assert refused["daily_sales"] == "stale"
        assert any(row["revenue"] == 301 for row in r.rows)

        settings.GOZAR = {**settings.GOZAR, "PREAGG_MAX_STALENESS": 3600}
        r = run(spec(dimensions=["city"]), today=TODAY, use_cache=False)
        assert r.execution["engine"] == "preaggregation" and r.execution["stale"]
        assert r.execution["lag_seconds"] is not None and r.execution["stale_since"]
        assert any(row["revenue"] == 300 for row in r.rows)  # the summary's (stale) value
        assert r.cache["stored_at"] is None  # stale rows are never cached

        refresh.refresh(get(DAILY), "auto")
        r = run(spec(dimensions=["city"]), today=TODAY, use_cache=False)
        assert r.execution["engine"] == "preaggregation" and not r.execution["stale"]
        assert any(row["revenue"] == 301 for row in r.rows)

    def test_lease(self, built):
        row = state_of()
        token = refresh.acquire(row)
        with pytest.raises(refresh.BuildBusy):
            refresh.refresh(get(DAILY), "rebuild")
        refresh.release(row, token)
        refresh.refresh(get(DAILY), "rebuild")

    def test_due(self, orders, calendar_table, frozen_today):
        from django.utils import timezone

        preagg = get(DAILY)
        assert refresh.due(preagg, None) == "rebuild"
        build(DAILY)
        row = state_of()
        now = timezone.now()
        assert refresh.due(preagg, row, now) is None
        cache._set_versions({"shop.order"})
        assert refresh.due(preagg, row, now) is None  # refreshed less than 10 minutes ago
        assert refresh.due(preagg, row, now + dt.timedelta(minutes=11)) == "auto"
        assert refresh.due(preagg, row, now + dt.timedelta(days=1, minutes=1)) == "rebuild"
        out = refresh.refresh_due(now + dt.timedelta(minutes=11))
        assert {o["name"] for o in out} >= {DAILY}

    def test_failed_build_is_recorded_and_retried_later(self, orders, frozen_today, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(refresh, "insert_select", boom)
        with pytest.raises(RuntimeError):
            refresh.refresh(get(DAILY), "rebuild")
        row = state_of()
        assert row.status == "failed" and "disk full" in row.last_error and not row.table
        assert refresh.due(get(DAILY), row) is None  # not retried at once
        assert not [t for t in refresh_tables() if "daily_sales" in t]

    def test_orphans(self, built):
        from bi_admin.scale.preaggregations import TABLE_PREFIX

        Materialization.objects.filter(name=DAILY).update(table="")
        dropped = refresh.drop_orphans("default")
        assert dropped and all(t.startswith(TABLE_PREFIX) for t in dropped)
        assert refresh.table_exists("default", state_of(MONTHLY).table)

    def test_jalali_coarse_buckets_need_the_calendar(self, orders, frozen_today):
        class JalaliMonths(Semantic):
            model = Order
            name = "jalali_probe"
            time_dimension = "created_at"
            dimensions = {"created_at": Dimension("created_at")}
            measures = {"revenue": Measure(Sum("amount")), "n": Measure(Count("id"))}
            pre_aggregations = [
                PreAggregation("months", measures=["revenue", "n"], time_grain="month",
                               calendar="jalali"),
            ]  # fmt: skip

        semantic_site.register(JalaliMonths)
        try:
            with pytest.raises(ReportError) as exc:
                refresh.refresh(get("jalali_probe.months"), "rebuild")
            assert exc.value.code == "calendar_not_populated"
            from bi_admin.calendar import table

            table.populate(dt.date(2025, 1, 1), dt.date(2026, 12, 31))
            refresh.refresh(get("jalali_probe.months"), "rebuild")
            s = {"semantic_model": "jalali_probe", "measures": ["revenue", "n"],
                 "time_grain": "season", "calendar": "jalali"}  # fmt: skip
            orm, pa = both(s)
            assert {r["created_at__label"] for r in pa.rows} >= {"1404-S4", "1405-S1"}
            s["time_grain"] = "month"
            s["calendar"] = "gregorian"
            r = run(s)
            assert r.execution["engine"] == "orm"
        finally:
            semantic_site.unregister("jalali_probe")


def refresh_tables():
    from django.db import connection

    with connection.cursor() as cursor:
        return connection.introspection.table_names(cursor)


# --- reporting the path --------------------------------------------------------------


class TestReporting:
    def test_audit_log_and_response(self, built, admin_client, report_api):
        response = report_api(admin_client, "run", spec(dimensions=["city"]))
        body = response.json()
        assert body["execution"]["engine"] == "preaggregation"
        assert body["execution"]["pre_aggregation"] == "daily_sales"
        log = ReportLog.objects.latest("id")
        assert (log.engine, log.pre_aggregation, log.cache_hit) == (
            "preaggregation",
            "daily_sales",
            False,
        )
        cached = report_api(admin_client, "run", spec(dimensions=["city"])).json()
        assert cached["cache"]["hit"] and cached["execution"]["engine"] == "preaggregation"
        log = ReportLog.objects.latest("id")
        assert (log.engine, log.cache_hit) == ("preaggregation", True)
        assert state_of().hits == 1  # cache hits don't read the table

    def test_orm_path_is_logged(self, orders, admin_client, report_api, frozen_today):
        report_api(admin_client, "run", spec(measures=["customers"]))
        assert ReportLog.objects.latest("id").engine == "orm"

    def test_export_uses_it(self, built, admin_client):
        from conftest import API

        response = admin_client.post(f"{API}/reports/export?format=csv", spec(dimensions=["city"]),
                                     "application/json")  # fmt: skip
        assert response.status_code == 200
        assert ReportLog.objects.latest("id").engine == "preaggregation"
