"""M2: ReportSpec validation and the ORM compiler, against hand-computed numbers.

The ``orders`` fixture (tests/conftest.py) documents the nine orders every
expected value below is derived from.
"""

import datetime as dt

import pytest
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from pydantic import ValidationError

from bi_admin.engine import ReportError, ReportSpec, SpecValidationError, compile_report, run
from bi_admin.engine import validate as validate_spec
from bi_admin.semantic import Dimension, Measure, Semantic, register, semantic_site
from shop.models import Order

M = "order_metrics"
TODAY = dt.date(2026, 10, 18)


def spec(**kw):
    return {"semantic_model": M, "measures": ["revenue"], **kw}


def errors_of(**kw) -> list[tuple[str, str]]:
    with pytest.raises(SpecValidationError) as exc:
        validate_spec(spec(**kw), today=TODAY)
    return [(e.code, e.path) for e in exc.value.errors]


def rows(**kw):
    return run(spec(**kw), today=TODAY).rows


def table(result_rows, *keys):
    return [tuple(r[k] for k in keys) for r in result_rows]


# --- ReportSpec schema -------------------------------------------------------------


def test_spec_defaults_and_strictness():
    s = ReportSpec(semantic_model=M, measures=["revenue"])
    assert (s.calendar, s.limit, s.chart, s.dimensions, s.filters) == (
        "jalali",
        1000,
        "table",
        [],
        [],
    )
    with pytest.raises(ValidationError):
        ReportSpec(semantic_model=M, measures=["revenue"], colour="red")  # extra keys forbidden
    with pytest.raises(ValidationError):
        ReportSpec(semantic_model=M, measures=["revenue"], time_grain="hour")
    with pytest.raises(ValidationError):
        ReportSpec.model_validate(spec(filters=[{"field": "status", "op": "like"}]))
    schema = ReportSpec.model_json_schema()  # doubles as the LLM output schema
    assert "semantic_model" in schema["required"] and schema["additionalProperties"] is False


# --- validator ---------------------------------------------------------------------


def test_valid_spec_is_normalised():
    report = validate_spec(
        spec(
            measures=["revenue", "orders"],
            dimensions=["city"],
            time_grain="month",
            relative_period="last_3_months",
            filters=[{"field": "status", "op": "eq", "value": "پرداخت‌شده"}],
            sort=[{"field": "orders", "direction": "asc"}],
            limit=20,
        ),
        today=TODAY,
    )
    assert [g.key for g in report.groups] == ["created_at", "city"]  # time bucket first
    assert [g.grain for g in report.groups] == ["month", None]
    assert report.time_dimension.key == "created_at" and report.time_grain == "month"
    assert (report.period.start, report.period.end) == (dt.date(2026, 6, 22), dt.date(2026, 9, 22))
    assert report.filters[0].value == "paid"  # choice label mapped to its value
    assert report.sort == [("orders", "asc")] and report.limit == 20
    assert report.keys == ["created_at", "city", "revenue", "orders"]


def test_unknown_semantic_model_stops_early():
    with pytest.raises(SpecValidationError) as exc:
        validate_spec({"semantic_model": "order_metric", "measures": ["nope"]})
    (err,) = exc.value.errors
    assert (err.code, err.path, err.suggestions) == (
        "unknown_semantic_model",
        "semantic_model",
        ["order_metrics"],
    )


def test_all_errors_are_collected_at_once():
    assert errors_of(
        measures=["revenu", "city", "revenue", "revenue"],
        dimensions=["town", "revenue", "status", "status"],
        filters=[{"field": "aov", "op": "gte", "value": 3}],
        sort=[{"field": "province"}, {"field": "nope"}],
        relative_period="last_forever",
        compare_to="previous_period",
        limit=0,
        clarification_needed="Which revenue?",
    ) == [
        ("clarification_needed", "clarification_needed"),
        ("unknown_measure", "measures[0]"),
        ("not_a_measure", "measures[1]"),
        ("duplicate", "measures[3]"),
        ("unknown_dimension", "dimensions[0]"),
        ("not_a_dimension", "dimensions[1]"),
        ("duplicate", "dimensions[3]"),
        ("unknown_relative_period", "relative_period"),
        ("measure_filter_not_supported", "filters[0].field"),
        ("sort_field_not_in_report", "sort[0].field"),
        ("unknown_field", "sort[1].field"),
        ("invalid_limit", "limit"),
        ("not_supported", "compare_to"),
    ]


def test_suggestions_use_labels_synonyms_and_fuzzy_matching():
    with pytest.raises(SpecValidationError) as exc:
        validate_spec(
            spec(measures=["فروش", "revenu", "میانگين سبد"], dimensions=["شهر", "town", "استان"])
        )
    suggestions = {e.path: e.suggestions for e in exc.value.errors}
    assert suggestions == {
        "measures[0]": ["revenue"],  # exact synonym
        "measures[1]": ["revenue"],  # typo
        "measures[2]": ["aov"],  # Arabic ي normalised to Persian ی
        "dimensions[0]": ["city"],
        "dimensions[1]": ["city"],  # English synonym
        "dimensions[2]": ["province"],
    }


def test_no_measures_and_dimension_cap(settings):
    assert errors_of(measures=[]) == [("no_measures", "measures")]
    settings.GOZAR = {"REPORT_MAX_DIMENSIONS": 2}
    assert errors_of(dimensions=["city", "status"], time_grain="month") == [
        ("too_many_dimensions", "dimensions")
    ]


def test_limit_cap(settings):
    settings.GOZAR = {"REPORT_MAX_ROWS": 50}
    assert errors_of(limit=51) == [("limit_exceeded", "limit")]
    assert validate_spec(spec(limit=None)).limit == 50
    assert validate_spec(spec(limit=50)).limit == 50


def test_time_dimension_rules():
    assert errors_of(time_dimension="status") == [("not_a_time_dimension", "time_dimension")]
    assert errors_of(time_dimension="when") == [("unknown_dimension", "time_dimension")]
    report = validate_spec(spec(time_dimension="paid_at", time_grain="year"))
    assert [(g.key, g.grain) for g in report.groups] == [("paid_at", "year")]
    # a time dimension listed among dimensions is bucketed (default: day), not duplicated
    report = validate_spec(spec(dimensions=["status", "created_at"]))
    assert [(g.key, g.grain) for g in report.groups] == [("status", None), ("created_at", "day")]
    report = validate_spec(spec(dimensions=["created_at"], time_grain="season"))
    assert [(g.key, g.grain) for g in report.groups] == [("created_at", "season")]


def test_semantic_model_without_time_dimension():
    class NoTime(Semantic):
        model = Order
        dimensions = {"status": Dimension("status")}
        measures = {"n": Measure(Count("id"))}

    register(NoTime)
    try:
        with pytest.raises(SpecValidationError) as exc:
            validate_spec({"semantic_model": "no_time", "measures": ["n"], "time_grain": "month"})
        assert exc.value.codes == ["no_time_dimension"]
    finally:
        semantic_site.unregister("no_time")


@pytest.mark.parametrize(
    "flt, code, path",
    [
        ({"field": "city", "op": "gte", "value": "a"}, "invalid_operator", "filters[0].op"),
        ({"field": "amount", "op": "contains", "value": "1"}, "invalid_operator", "filters[0].op"),
        ({"field": "status", "op": "eq", "value": "lost"}, "invalid_choice", "filters[0].value"),
        ({"field": "status", "op": "in", "value": []}, "invalid_value", "filters[0].value"),
        ({"field": "status", "op": "in", "value": "paid"}, "invalid_value", "filters[0].value"),
        ({"field": "status", "op": "eq", "value": ["paid"]}, "invalid_value", "filters[0].value"),
        ({"field": "status", "op": "eq"}, "invalid_value", "filters[0].value"),
        ({"field": "amount", "op": "between", "value": [1]}, "invalid_value", "filters[0].value"),
        (
            {"field": "amount", "op": "between", "value": [9, 1]},
            "invalid_value",
            "filters[0].value",
        ),
        ({"field": "amount", "op": "gte", "value": "lots"}, "invalid_value", "filters[0].value"),
        ({"field": "amount", "op": "gte", "value": True}, "invalid_value", "filters[0].value"),
        (
            {"field": "created_at", "op": "gte", "value": "soon"},
            "invalid_value",
            "filters[0].value",
        ),
        (
            {"field": "created_at", "op": "between", "value": ["1405/01/01", "x"]},
            "invalid_value",
            "filters[0].value[1]",
        ),
        (
            {"field": "paid_at", "op": "is_null", "value": "yes"},
            "invalid_value",
            "filters[0].value",
        ),
        ({"field": "town", "op": "eq", "value": "x"}, "unknown_dimension", "filters[0].field"),
    ],
)
def test_filter_errors(flt, code, path):
    assert errors_of(filters=[flt]) == [(code, path)]


def test_filter_values_are_coerced():
    report = validate_spec(
        spec(
            filters=[
                {"field": "created_at", "op": "between", "value": ["1405/06/01", "2026-09-22"]},
                {"field": "amount", "op": "gte", "value": "۳۰۰"},
                {"field": "status", "op": "in", "value": ["paid", "مرجوعی"]},
                {"field": "paid_at", "op": "is_null"},
            ]
        )
    )
    values = [f.value for f in report.filters]
    assert values[0] == [dt.date(2026, 8, 23), dt.date(2026, 9, 22)]
    assert values[1] == 300
    assert values[2] == ["paid", "refunded"]
    assert values[3] is True


def test_validator_rechecks_paths_against_the_registry(settings):
    from bi_admin.discovery import reset_registry

    semantic_site.get(M)  # bound while shop.city is visible
    settings.GOZAR = {"EXCLUDE_MODELS": ["shop.city"]}
    reset_registry()
    assert errors_of(dimensions=["city"]) == [("invalid_lookup", "dimensions[0]")]


# --- compiler: aggregates and dimensions --------------------------------------------


@pytest.mark.django_db
def test_kpi_without_dimensions(orders):
    result = run(spec(measures=["revenue", "orders", "paid_orders", "aov", "customers"]))
    assert result.rows == [
        {"revenue": 2700, "orders": 9, "paid_orders": 6, "aov": 450, "customers": 4}
    ]
    assert not result.truncated


@pytest.mark.django_db
def test_group_by_city_sorted_by_first_measure(orders):
    assert table(
        rows(measures=["revenue", "orders"], dimensions=["city"]), "city", "revenue", "orders"
    ) == [
        ("تهران", 1800, 5),
        ("مشهد", 600, 2),
        ("شیراز", 300, 2),
    ]


@pytest.mark.django_db
def test_group_by_two_dimensions_with_sort_and_limit(orders):
    result = run(
        spec(
            measures=["orders", "revenue"],
            dimensions=["province", "status"],
            sort=[{"field": "revenue", "direction": "desc"}],
            limit=3,
        )
    )
    assert table(result.rows, "province", "status", "orders", "revenue") == [
        ("تهران", "paid", 4, 1800),
        ("خراسان رضوی", "paid", 1, 600),
        ("فارس", "paid", 1, 300),
    ]
    assert result.truncated  # 6 (province, status) groups exist


@pytest.mark.django_db
def test_sort_ascending_and_nulls_last(orders):
    got = rows(
        measures=["revenue"], dimensions=["status"], sort=[{"field": "revenue", "direction": "asc"}]
    )
    assert table(got, "status", "revenue") == [
        ("paid", 2700),
        ("cancelled", None),  # SUM over no paid rows is NULL; NULLs sort last
        ("pending", None),
        ("refunded", None),
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "flt, expected_orders",
    [
        ({"field": "status", "op": "in", "value": ["paid", "refunded"]}, 7),
        ({"field": "status", "op": "eq", "value": "پرداخت‌شده"}, 6),
        ({"field": "status", "op": "neq", "value": "paid"}, 3),
        ({"field": "status", "op": "not_in", "value": ["paid", "pending"]}, 2),
        ({"field": "city", "op": "contains", "value": "تهر"}, 5),
        ({"field": "province", "op": "eq", "value": "فارس"}, 2),
        ({"field": "paid_at", "op": "is_null", "value": True}, 2),
        ({"field": "paid_at", "op": "is_null", "value": False}, 7),
        ({"field": "amount", "op": "gte", "value": 300}, 5),
        ({"field": "amount", "op": "lte", "value": 100}, 3),
        ({"field": "amount", "op": "between", "value": [100, 300]}, 3),
        ({"field": "created_at", "op": "between", "value": ["1405/06/01", "1405/06/31"]}, 2),
        ({"field": "created_at", "op": "eq", "value": "2026-09-23"}, 1),
        ({"field": "created_at", "op": "neq", "value": "2026-09-23"}, 8),
        ({"field": "created_at", "op": "gte", "value": "1405/07/01"}, 3),
        ({"field": "created_at", "op": "lte", "value": "1404/12/29"}, 2),
        ({"field": "created_at", "op": "in", "value": ["2026-09-22", "1405/07/01"]}, 2),
        ({"field": "created_at", "op": "not_in", "value": ["2026-09-22", "2026-09-23"]}, 7),
    ],
)
def test_filters(orders, flt, expected_orders):
    assert rows(measures=["orders"], filters=[flt]) == [{"orders": expected_orders}]


@pytest.mark.django_db
def test_filters_combine_with_and(orders):
    got = rows(
        measures=["orders", "revenue"],
        filters=[
            {"field": "city", "op": "eq", "value": "تهران"},
            {"field": "created_at", "op": "gte", "value": "2026-01-01"},
        ],
    )
    assert got == [{"orders": 4, "revenue": 800}]  # #1, #2, #3, #7


# --- compiler: time grains -------------------------------------------------------


@pytest.mark.django_db
def test_jalali_month_grain(orders, calendar_table):
    got = rows(measures=["revenue", "orders"], time_grain="month")
    assert table(got, "created_at", "created_at__label", "revenue", "orders") == [
        ("2025-09-23", "1404-07", 1000, 1),
        ("2026-02-20", "1404-12", 500, 1),  # 1404/12/29, the last day of the year
        ("2026-03-21", "1405-01", 600, 1),  # Nowruz, one day later
        ("2026-07-23", "1405-05", None, 1),  # only a refunded order
        ("2026-08-23", "1405-06", 100, 2),  # #1 at 23:30 on the last day of Shahrivar
        ("2026-09-23", "1405-07", 500, 3),  # #2 at 00:30 on 1 Mehr (still Sep 22 in UTC)
    ]


@pytest.mark.django_db
def test_gregorian_month_grain(orders):
    got = rows(measures=["revenue", "orders"], time_grain="month", calendar="gregorian")
    assert table(got, "created_at", "created_at__label", "revenue", "orders") == [
        ("2025-10-01", "2025-10", 1000, 1),
        ("2026-03-01", "2026-03", 1100, 2),
        ("2026-08-01", "2026-08", None, 2),
        ("2026-09-01", "2026-09", 300, 3),
        ("2026-10-01", "2026-10", 300, 1),
    ]


@pytest.mark.django_db
def test_jalali_season_grain(orders, calendar_table):
    got = rows(measures=["revenue", "orders"], time_grain="season")
    assert table(got, "created_at", "created_at__label", "revenue", "orders") == [
        ("2025-09-23", "1404-S3", 1000, 1),
        ("2025-12-22", "1404-S4", 500, 1),
        ("2026-03-21", "1405-S1", 600, 1),
        ("2026-06-22", "1405-S2", 100, 3),  # summer ends 1405/06/31 23:59 Tehran time
        ("2026-09-23", "1405-S3", 500, 3),
    ]


@pytest.mark.django_db
def test_gregorian_season_is_quarter(orders):
    got = rows(measures=["revenue", "orders"], time_grain="season", calendar="gregorian")
    assert table(got, "created_at__label", "revenue", "orders") == [
        ("2025-Q4", 1000, 1),
        ("2026-Q1", 1100, 2),
        ("2026-Q3", 300, 5),
        ("2026-Q4", 300, 1),
    ]


@pytest.mark.django_db
def test_year_grains(orders, calendar_table):
    jalali = rows(measures=["revenue", "orders"], time_grain="year")
    assert table(jalali, "created_at", "created_at__label", "revenue", "orders") == [
        ("2025-03-21", "1404", 1500, 2),
        ("2026-03-21", "1405", 1200, 7),
    ]
    gregorian = rows(measures=["revenue", "orders"], time_grain="year", calendar="gregorian")
    assert table(gregorian, "created_at__label", "revenue", "orders") == [
        ("2025", 1000, 1),
        ("2026", 1700, 8),
    ]


@pytest.mark.django_db
def test_week_grains(orders, calendar_table):
    flt = [{"field": "created_at", "op": "between", "value": ["2026-09-19", "2026-10-02"]}]
    jalali = rows(measures=["orders"], time_grain="week", filters=flt)
    assert table(jalali, "created_at", "created_at__label", "orders") == [
        ("2026-09-19", "1405-06-28", 2),  # Saturday-start weeks
        ("2026-09-26", "1405-07-04", 2),
    ]
    gregorian = rows(measures=["orders"], time_grain="week", filters=flt, calendar="gregorian")
    assert table(gregorian, "created_at", "created_at__label", "orders") == [
        ("2026-09-21", "2026-W39", 2),  # ISO weeks start Monday
        ("2026-09-28", "2026-W40", 2),
    ]


@pytest.mark.django_db
def test_day_grain_uses_local_time(orders):
    got = rows(
        measures=["revenue"],
        time_grain="day",
        filters=[{"field": "created_at", "op": "between", "value": ["2026-09-22", "2026-09-23"]}],
    )
    assert table(got, "created_at", "created_at__label", "revenue") == [
        ("2026-09-22", "1405-06-31", 100),
        ("2026-09-23", "1405-07-01", 200),
    ]
    gregorian = rows(measures=["revenue"], time_grain="day", calendar="gregorian", limit=1)
    assert table(gregorian, "created_at__label", "revenue") == [("2025-10-18", 1000)]


@pytest.mark.django_db
def test_time_grain_with_category_dimension(orders, calendar_table):
    got = rows(
        measures=["orders"],
        dimensions=["city"],
        time_grain="season",
        filters=[{"field": "created_at", "op": "gte", "value": "1405/04/01"}],
    )
    assert table(got, "created_at__label", "city", "orders") == [
        ("1405-S2", "تهران", 1),
        ("1405-S2", "شیراز", 1),
        ("1405-S2", "مشهد", 1),
        ("1405-S3", "تهران", 2),
        ("1405-S3", "شیراز", 1),
    ]


@pytest.mark.django_db
def test_grain_on_nullable_time_dimension(orders, calendar_table):
    got = rows(measures=["orders"], time_dimension="paid_at", time_grain="year")
    # unpaid orders form their own (null) bucket, sorted last
    assert table(got, "paid_at", "paid_at__label", "orders") == [
        ("2025-03-21", "1404", 2),
        ("2026-03-21", "1405", 5),
        (None, None, 2),
    ]
    paid = rows(
        measures=["orders"],
        dimensions=["paid_at"],
        time_grain="year",
        filters=[{"field": "paid_at", "op": "is_null", "value": False}],
    )
    assert table(paid, "paid_at__label", "orders") == [("1404", 2), ("1405", 5)]


@pytest.mark.django_db
def test_jalali_grain_requires_calendar_rows(orders):
    with pytest.raises(ReportError) as exc:
        run(spec(time_grain="month"))
    assert exc.value.status == 409 and exc.value.payload["code"] == "calendar_not_populated"
    assert exc.value.payload["start"] == "2025-10-18"
    assert exc.value.payload["end"] == "2026-10-01"
    run(spec(time_grain="day"))  # day buckets need no calendar rows
    run(spec(time_grain="month", calendar="gregorian"))


# --- compiler: relative periods (today = 1405/07/26) --------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "period, calendar, revenue, orders_",
    [
        ("this_month", "jalali", 500, 3),  # #2, #3, #4
        ("last_month", "jalali", 100, 2),  # Shahrivar: #5 and #1 (23:30 on its last day)
        ("last_3_months", "jalali", 100, 3),  # Tir–Shahrivar: #6, #5, #1
        ("this_season", "jalali", 500, 3),
        ("last_season", "jalali", 100, 3),
        ("this_year", "jalali", 1200, 7),
        ("last_year", "jalali", 1500, 2),  # 1404: #9 and #7 (on 1404/12/29)
        ("past_30_days", "jalali", 600, 4),
        ("last_week", "jalali", None, 0),
        ("last_month", "gregorian", 300, 3),  # September: #1, #2, #3
        ("this_year", "gregorian", 1700, 8),
        ("last_year", "gregorian", 1000, 1),
    ],
)
def test_relative_periods(orders, frozen_today, period, calendar, revenue, orders_):
    got = run(spec(measures=["revenue", "orders"], relative_period=period, calendar=calendar))
    assert got.rows == [{"revenue": revenue, "orders": orders_}]


@pytest.mark.django_db
def test_relative_period_with_grain_and_period_metadata(orders, calendar_table, frozen_today):
    result = run(spec(measures=["orders"], time_grain="month", relative_period="last_3_months"))
    assert table(result.rows, "created_at__label", "orders") == [("1405-05", 1), ("1405-06", 2)]
    payload = result.as_dict()
    assert payload["period"]["start"] == "2026-06-22" and payload["period"]["end"] == "2026-09-22"
    assert payload["period"]["start_jalali"] == "1405-04-01"
    assert payload["time_grain"] == "month" and payload["calendar"] == "jalali"
    assert [c["key"] for c in payload["columns"]] == ["created_at", "orders"]
    assert payload["columns"][0]["label_key"] == "created_at__label"


@pytest.mark.django_db
def test_relative_period_on_another_time_dimension(orders, frozen_today):
    got = rows(measures=["orders"], time_dimension="paid_at", relative_period="last_month")
    assert got == [{"orders": 1}]  # #1 was paid in Shahrivar; #5 was never paid


# --- compiler: expression dimensions and SQL safety ----------------------------------


@pytest.mark.django_db
def test_expression_dimensions(orders, calendar_table):
    class OrderDays(Semantic):
        model = Order
        dimensions = {
            "day": Dimension(TruncDate("created_at"), type="time"),
            "status": Dimension("status"),
        }
        measures = {"total": Measure(Sum("amount"))}

    register(OrderDays)
    try:
        got = run(
            {
                "semantic_model": "order_days",
                "measures": ["total"],
                "time_grain": "season",
                "filters": [{"field": "day", "op": "gte", "value": "1405/04/01"}],
            }
        ).rows
        assert table(got, "day__label", "total") == [("1405-S2", 570), ("1405-S3", 550)]
    finally:
        semantic_site.unregister("order_days")


@pytest.mark.django_db
def test_values_are_parameters_not_sql(orders):
    evil = "x'); DROP TABLE shop_order; --"
    assert rows(measures=["orders"], filters=[{"field": "city", "op": "eq", "value": evil}]) == [
        {"orders": 0}
    ]
    assert Order.objects.count() == 9
    compiled = compile_report(validate_spec(spec(dimensions=["city"])))
    assert "DROP" not in compiled.sql and not compiled.aggregate


@pytest.mark.django_db
def test_semantic_get_queryset_is_respected(orders, monkeypatch):
    sem = semantic_site.get(M)
    monkeypatch.setattr(sem, "get_queryset", lambda request: Order.objects.filter(status="paid"))
    assert rows(measures=["orders"]) == [{"orders": 6}]


@pytest.mark.django_db
def test_measure_sort_keeps_time_buckets_as_tie_breaker(orders):
    got = rows(
        measures=["orders"],
        time_grain="month",
        calendar="gregorian",
        sort=[{"field": "orders", "direction": "desc"}],
    )
    assert table(got, "created_at__label", "orders") == [
        ("2026-09", 3),
        ("2026-03", 2),  # ties on orders are broken chronologically
        ("2026-08", 2),
        ("2025-10", 1),
        ("2026-10", 1),
    ]
