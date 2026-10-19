"""ReportSpec.compare_to: comparison periods in both calendars, hand-computed.

Every order below is paid and its amount is a distinct power of two, so each sum
names exactly the orders it contains.

#  amount  city     date (12:00 Tehran)  Jalali
A      1   Tehran   2024-02-29           1402/12/10  (Gregorian leap day)
J    512   Shiraz   2024-02-28           1402/12/09
B      2   Shiraz   2024-12-31           1403/10/11
C      4   Tehran   2025-01-01           1403/10/12
D      8   Shiraz   2025-02-28           1403/12/10
E     16   Tehran   2025-03-20           1403/12/30  (leap Esfand)
F     32   Tehran   2025-12-31           1404/10/10
G     64   Shiraz   2026-01-01           1404/10/11
H    128   Tehran   2026-03-20           1404/12/29  (last day of 1404)
I    256   Shiraz   2026-03-21           1405/01/01
"""

import datetime as dt
from decimal import Decimal

import pytest
from conftest import tehran

from bi_admin.calendar import jalali, periods
from bi_admin.engine import SpecValidationError, run, validate
from bi_admin.engine.drilldown import drilldown

ORDERS = {
    "A": (1, "Tehran", (2024, 2, 29)),
    "J": (512, "Shiraz", (2024, 2, 28)),
    "B": (2, "Shiraz", (2024, 12, 31)),
    "C": (4, "Tehran", (2025, 1, 1)),
    "D": (8, "Shiraz", (2025, 2, 28)),
    "E": (16, "Tehran", (2025, 3, 20)),
    "F": (32, "Tehran", (2025, 12, 31)),
    "G": (64, "Shiraz", (2026, 1, 1)),
    "H": (128, "Tehran", (2026, 3, 20)),
    "I": (256, "Shiraz", (2026, 3, 21)),
}


def total(*names: str) -> int:
    return sum(ORDERS[n][0] for n in names)


@pytest.fixture
def history(db):
    from bi_admin.calendar import table
    from shop.models import City, Customer, Order

    table.populate(dt.date(2023, 1, 1), dt.date(2026, 12, 31))
    cities = {
        "Tehran": City.objects.create(name="Tehran", name_en="Tehran", province="Tehran"),
        "Shiraz": City.objects.create(name="Shiraz", name_en="Shiraz", province="Fars"),
    }
    customers = {
        name: Customer.objects.create(name=f"c-{name}", city=city, created_at=tehran(2023, 1, 1))
        for name, city in cities.items()
    }
    for amount, city, day in ORDERS.values():
        Order.objects.create(
            customer=customers[city],
            status="paid",
            amount=Decimal(amount),
            created_at=tehran(*day),
            paid_at=tehran(*day),
        )


def report(today, **spec):
    body = {"semantic_model": "order_metrics", "measures": ["revenue"], **spec}
    return run(body, today=today).as_dict()


def test_jalali_dates_of_the_fixture():
    assert jalali.bucket_label(dt.date(2025, 3, 20), "day", "jalali") == "1403-12-30"
    assert jalali.bucket_label(dt.date(2026, 3, 20), "day", "jalali") == "1404-12-29"
    assert jalali.bucket_label(dt.date(2026, 3, 21), "day", "jalali") == "1405-01-01"


# --- period arithmetic ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "name", "calendar", "today", "expected"),
    [
        # to-date periods compare with the same days of the previous unit
        ("previous_period", "this_month", "gregorian", "2026-10-18", ("2026-09-01", "2026-09-18")),
        ("previous_period", "this_month", "gregorian", "2026-03-31", ("2026-02-01", "2026-02-28")),
        # complete units keep ending on a unit boundary (Aug has 31 days)
        ("previous_period", "last_month", "gregorian", "2026-10-18", ("2026-08-01", "2026-08-31")),
        (
            "previous_period",
            "last_3_months",
            "gregorian",
            "2026-10-18",
            ("2026-04-01", "2026-06-30"),
        ),
        ("previous_period", "last_year", "gregorian", "2026-01-10", ("2024-01-01", "2024-12-31")),
        (
            "previous_period",
            "past_30_days",
            "gregorian",
            "2026-10-18",
            ("2026-08-20", "2026-09-18"),
        ),
        ("previous_period", "last_week", "gregorian", "2026-10-18", ("2026-09-28", "2026-10-04")),
        ("previous_period", "today", "gregorian", "2026-01-01", ("2025-12-31", "2025-12-31")),
        # Jalali: Mehr 1405 to date (26 days) → Shahrivar 1405, 1–26
        ("previous_period", "this_month", "jalali", "2026-10-18", ("2026-08-23", "2026-09-17")),
        # last Jalali month = Shahrivar (31 days) → Mordad (31 days)
        ("previous_period", "last_month", "jalali", "2026-10-18", ("2026-07-23", "2026-08-22")),
        # last Jalali season = summer 1405 → spring 1405
        ("previous_period", "last_season", "jalali", "2026-10-18", ("2026-03-21", "2026-06-21")),
        # crossing Nowruz: Farvardin 1405 → Esfand 1404 (29 days)
        ("previous_period", "last_month", "jalali", "2026-04-25", ("2026-02-20", "2026-03-20")),
        (
            "same_period_last_year",
            "this_year",
            "gregorian",
            "2026-10-18",
            ("2025-01-01", "2025-10-18"),
        ),
        (
            "same_period_last_year",
            "last_month",
            "gregorian",
            "2025-03-05",
            ("2024-02-01", "2024-02-29"),
        ),
        ("same_period_last_year", "today", "gregorian", "2028-02-29", ("2027-02-28", "2027-02-28")),
        # weeks move 52 weeks so weekdays line up
        (
            "same_period_last_year",
            "this_week",
            "gregorian",
            "2026-10-18",
            ("2025-10-13", "2025-10-19"),
        ),
        # Esfand 1404 (29 days) a year back is Esfand 1403, which has 30
        (
            "same_period_last_year",
            "last_month",
            "jalali",
            "2026-04-01",
            ("2025-02-19", "2025-03-20"),
        ),
        (
            "same_period_last_year",
            "this_year",
            "jalali",
            "2026-10-18",
            ("2025-03-21", "2025-10-18"),
        ),
    ],
)
def test_comparison_ranges(kind, name, calendar, today, expected):
    period = periods.resolve(name, calendar, dt.date.fromisoformat(today))
    found = periods.comparison(kind, period.start, period.end, calendar, name)
    assert (found.start.isoformat(), found.end.isoformat()) == expected


@pytest.mark.parametrize(
    ("kind", "calendar", "start", "end", "expected"),
    [
        # whole months move by their count of months
        ("previous_period", "gregorian", "2026-01-01", "2026-02-28", ("2025-11-01", "2025-12-31")),
        # Jalali: Dey–Esfand 1404 → Mehr–Azar 1404
        ("previous_period", "jalali", "2025-12-22", "2026-03-20", ("2025-09-23", "2025-12-21")),
        # anything else: the same number of days just before
        ("previous_period", "gregorian", "2026-01-01", "2026-03-20", ("2025-10-14", "2025-12-31")),
        ("previous_period", "gregorian", "2026-03-01", "2026-03-01", ("2026-02-28", "2026-02-28")),
        (
            "same_period_last_year",
            "gregorian",
            "2025-02-01",
            "2025-02-28",
            ("2024-02-01", "2024-02-29"),
        ),
        (
            "same_period_last_year",
            "gregorian",
            "2024-02-10",
            "2024-02-29",
            ("2023-02-10", "2023-02-28"),
        ),
        (
            "same_period_last_year",
            "jalali",
            "2025-03-01",
            "2025-03-20",
            ("2024-03-01", "2024-03-19"),
        ),
    ],
)
def test_comparison_of_custom_ranges(kind, calendar, start, end, expected):
    found = periods.comparison(
        kind, dt.date.fromisoformat(start), dt.date.fromisoformat(end), calendar
    )
    assert (found.start.isoformat(), found.end.isoformat()) == expected


def test_unknown_comparison_kind():
    with pytest.raises(periods.PeriodError):
        periods.comparison("next_year", dt.date(2026, 1, 1), dt.date(2026, 1, 2))


# --- reports ---------------------------------------------------------------------------


def test_kpi_same_period_last_year_gregorian(history):
    # Jan 1–15 2026 vs Jan 1–15 2025
    data = report(
        dt.date(2026, 1, 15), relative_period="this_month", compare_to="same_period_last_year"
    )
    assert data["rows"] == [
        {
            "revenue": total("G"),
            "revenue__compare": total("C"),
            "revenue__change": total("G") - total("C"),
            "revenue__change_pct": 15.0,
        }
    ]
    assert data["compare_to"] == "same_period_last_year"
    assert data["comparison"] == {
        "kind": "same_period_last_year",
        "calendar": "gregorian",
        "start": "2025-01-01",
        "end": "2025-01-15",
    }
    keys = [(c["key"], c["role"], c.get("format")) for c in data["columns"]]
    assert keys == [
        ("revenue", "measure", "currency"),
        ("revenue__compare", "comparison", "currency"),
        ("revenue__change", "comparison", "currency"),
        ("revenue__change_pct", "comparison", "percent"),
    ]
    labels = {c["key"]: c["labels"] for c in data["columns"]}
    assert labels["revenue__compare"]["en"] == "Revenue (same period last year)"
    assert labels["revenue__compare"]["fa"] == "درآمد (دوره مشابه سال قبل)"
    assert labels["revenue__change_pct"]["en"] == "Revenue change %"


def test_previous_year_across_the_year_boundary(history):
    data = report(dt.date(2026, 1, 10), relative_period="last_year", compare_to="previous_period")
    current, previous = total("C", "D", "E", "F"), total("A", "J", "B")
    (row,) = data["rows"]
    assert (row["revenue"], row["revenue__compare"]) == (current, previous)
    assert row["revenue__change"] == current - previous == -455
    assert row["revenue__change_pct"] == round((current - previous) / previous, 6)
    assert (data["comparison"]["start"], data["comparison"]["end"]) == ("2024-01-01", "2024-12-31")


def test_monthly_buckets_line_up_across_the_year_boundary(history):
    # Dec 2025 – Jan 15 2026 by month vs Dec 2024 – Jan 15 2025
    data = report(
        dt.date(2026, 1, 15),
        relative_period="past_2_months",
        time_grain="month",
        compare_to="same_period_last_year",
    )
    assert [
        (r["created_at"], r["revenue"], r["revenue__compare"], r["revenue__change"])
        for r in data["rows"]
    ] == [
        ("2025-12-01", total("F"), total("B"), total("F") - total("B")),
        ("2026-01-01", total("G"), total("C"), total("G") - total("C")),
    ]


def test_previous_period_by_month_moves_each_bucket(history):
    # Jan–Mar 2026 (to Mar 31) vs Oct–Dec 2025: only December has an order (F)
    data = report(
        dt.date(2026, 3, 31),
        relative_period="past_3_months",
        time_grain="month",
        compare_to="previous_period",
    )
    assert [(r["created_at"], r["revenue"], r["revenue__compare"]) for r in data["rows"]] == [
        ("2026-01-01", total("G"), None),
        ("2026-03-01", total("H", "I"), total("F")),  # March ↔ December
    ]
    assert data["rows"][0]["revenue__change"] is None
    assert data["rows"][0]["revenue__change_pct"] is None


def test_leap_day_prefers_the_exact_day(history):
    # Feb 2025 by day vs Feb 1–29 2024: 29 Feb 2024 (A) and 28 Feb 2024 (J) both
    # line up with 28 Feb 2025; the exact day (J) wins.
    data = report(
        None,
        time_grain="day",
        filters=[{"field": "created_at", "op": "between", "value": ["2025-02-01", "2025-02-28"]}],
        compare_to="same_period_last_year",
    )
    assert data["comparison"]["end"] == "2024-02-29"
    assert [(r["created_at"], r["revenue"], r["revenue__compare"]) for r in data["rows"]] == [
        ("2025-02-28", total("D"), total("J")),
    ]


def test_grouped_by_city(history):
    data = report(
        dt.date(2026, 1, 10),
        dimensions=["city"],
        relative_period="last_year",
        compare_to="previous_period",
    )
    rows = {r["city"]: (r["revenue"], r["revenue__compare"]) for r in data["rows"]}
    assert rows == {
        "Tehran": (total("C", "E", "F"), total("A")),
        "Shiraz": (total("D"), total("J", "B")),
    }


def test_custom_range_previous_period_uses_its_length(history):
    # Jan 1 – Mar 20 2026 (79 days) vs Oct 14 – Dec 31 2025
    data = report(
        None,
        filters=[{"field": "created_at", "op": "between", "value": ["2026-01-01", "2026-03-20"]}],
        compare_to="previous_period",
    )
    assert data["rows"][0]["revenue"] == total("G", "H")
    assert data["rows"][0]["revenue__compare"] == total("F")
    assert data["comparison"]["start"] == "2025-10-14"


def test_jalali_year_with_a_leap_esfand(history):
    # 1404 to its last day vs the whole of 1403, including 30 Esfand (E)
    data = report(
        dt.date(2026, 3, 20),
        relative_period="this_year",
        calendar="jalali",
        compare_to="same_period_last_year",
    )
    assert data["comparison"] == {
        "kind": "same_period_last_year",
        "calendar": "jalali",
        "start": "2024-03-20",
        "end": "2025-03-20",
        "start_jalali": "1403-01-01",
        "end_jalali": "1403-12-30",
    }
    (row,) = data["rows"]
    assert row["revenue"] == total("F", "G", "H")
    assert row["revenue__compare"] == total("B", "C", "D", "E")


def test_jalali_month_buckets_across_nowruz(history):
    # Dey–Esfand 1404 by Jalali month vs Dey–Esfand 1403
    data = report(
        dt.date(2026, 3, 20),
        relative_period="past_3_months",
        time_grain="month",
        calendar="jalali",
        compare_to="same_period_last_year",
    )
    assert [
        (r["created_at__label"], r["revenue"], r["revenue__compare"]) for r in data["rows"]
    ] == [
        ("1404-10", total("F", "G"), total("B", "C")),
        ("1404-12", total("H"), total("D", "E")),
    ]


def test_jalali_previous_period_across_nowruz(history):
    # Farvardin 1405 to date (1–1) vs Esfand 1404, 1–1: nothing; then whole months
    data = report(
        dt.date(2026, 4, 25),
        relative_period="last_month",
        calendar="jalali",
        compare_to="previous_period",
    )
    (row,) = data["rows"]
    assert row["revenue"] == total("I")  # Farvardin 1405
    assert row["revenue__compare"] == total("H")  # Esfand 1404
    assert row["revenue__change_pct"] == 1.0


def test_weekly_buckets_move_52_weeks(history):
    data = report(
        None,
        time_grain="week",
        filters=[{"field": "created_at", "op": "between", "value": ["2025-12-29", "2026-01-04"]}],
        compare_to="same_period_last_year",
    )
    # 52 weeks before the ISO week of 1 Jan 2026 is the week of 30 Dec 2024 (B, C)
    assert [(r["created_at"], r["revenue"], r["revenue__compare"]) for r in data["rows"]] == [
        ("2025-12-29", total("F", "G"), total("B", "C")),
    ]


def test_zero_comparison_has_no_percent(history):
    data = report(
        None,
        measures=["revenue", "orders"],
        filters=[{"field": "created_at", "op": "eq", "value": "2026-03-21"}],
        compare_to="previous_period",
    )
    (row,) = data["rows"]
    assert row["orders__compare"] == 1  # H, the day before
    assert row["orders__change"] == 0
    assert row["orders__change_pct"] == 0.0
    data = report(
        None,
        measures=["orders"],
        filters=[{"field": "created_at", "op": "eq", "value": "2026-03-22"}],
        compare_to="previous_period",
    )
    (row,) = data["rows"]
    assert (row["orders"], row["orders__compare"], row["orders__change"]) == (0, 1, -1)
    data = report(
        None,
        measures=["orders"],
        filters=[{"field": "created_at", "op": "eq", "value": "2026-03-21"}],
        compare_to="same_period_last_year",
    )
    (row,) = data["rows"]
    assert (row["orders__compare"], row["orders__change_pct"]) == (0, None)


def test_comparison_needs_a_period(history):
    with pytest.raises(SpecValidationError) as exc:
        validate(
            {
                "semantic_model": "order_metrics",
                "measures": ["revenue"],
                "compare_to": "previous_period",
            }
        )
    (err,) = exc.value.errors
    assert (err.code, err.path) == ("compare_needs_period", "compare_to")
    # a one-sided date filter isn't a period either
    with pytest.raises(SpecValidationError) as exc:
        validate(
            {
                "semantic_model": "order_metrics",
                "measures": ["revenue"],
                "filters": [{"field": "created_at", "op": "gte", "value": "2026-01-01"}],
                "compare_to": "previous_period",
            }
        )
    assert exc.value.codes == ["compare_needs_period"]


def test_comparison_is_cached_with_the_report(history):
    spec = {
        "semantic_model": "order_metrics",
        "measures": ["revenue"],
        "relative_period": "last_year",
        "compare_to": "previous_period",
    }
    first = run(spec, today=dt.date(2026, 1, 10))
    second = run(spec, today=dt.date(2026, 1, 10))
    assert second.cache["hit"] and second.rows == first.rows
    plain = run({**spec, "compare_to": None}, today=dt.date(2026, 1, 10))
    assert not plain.cache["hit"] and "revenue__compare" not in plain.rows[0]


def test_drilldown_into_a_comparison_value(history, admin_user, rf, monkeypatch):
    from bi_admin.engine import ReportSpec

    request = rf.get("/")
    request.user = admin_user
    monkeypatch.setattr(periods, "today", lambda: dt.date(2026, 3, 20))
    spec = {
        "semantic_model": "order_metrics",
        "measures": ["revenue"],
        "time_grain": "month",
        "calendar": "jalali",
        "relative_period": "past_3_months",
        "compare_to": "same_period_last_year",
    }
    cell = {"created_at": "2026-02-20"}  # Esfand 1404 ↔ Esfand 1403
    current = drilldown(ReportSpec(**spec), cell, "revenue", request)
    previous = drilldown(ReportSpec(**spec), cell, "revenue", request, comparison=True)
    assert current["count"] == 1  # H
    assert previous["count"] == 2  # D, E
    assert previous["comparison"] is True


def test_export_includes_the_comparison_columns(history):
    from bi_admin.engine.export import table

    result = run(
        {
            "semantic_model": "order_metrics",
            "measures": ["revenue"],
            "relative_period": "last_year",
            "compare_to": "previous_period",
        },
        today=dt.date(2026, 1, 10),
    )
    headers, rows, _columns = table(result)
    assert headers == [
        "Revenue",
        "Revenue (previous period)",
        "Revenue change",
        "Revenue change %",
    ]
    assert rows[0][:3] == [total("C", "D", "E", "F"), total("A", "J", "B"), -455]
