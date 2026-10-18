"""M2: Jalali helpers, the CalendarDay table + command, and relative periods."""

import datetime as dt
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from bi_admin.calendar import jalali, periods, table
from bi_admin.models import CalendarDay

D = dt.date

# --- jalali helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    "gregorian, expected",
    [
        (D(2026, 3, 21), (1405, 1, 1)),  # Nowruz 1405
        (D(2026, 3, 20), (1404, 12, 29)),  # 1404 is not leap: Esfand has 29 days
        (D(2025, 3, 20), (1403, 12, 30)),  # 1403 is leap
        (D(2026, 9, 22), (1405, 6, 31)),
        (D(2026, 9, 23), (1405, 7, 1)),
        (D(2026, 10, 18), (1405, 7, 26)),
    ],
)
def test_to_jalali(gregorian, expected):
    j = jalali.to_jalali(gregorian)
    assert (j.year, j.month, j.day) == expected
    assert jalali.to_gregorian(*expected) == gregorian


def test_month_lengths_and_seasons():
    assert [jalali.month_length(1405, m) for m in range(1, 13)] == [31] * 6 + [30] * 5 + [29]
    assert jalali.month_length(1403, 12) == 30
    assert [jalali.season_of(m) for m in (1, 3, 4, 6, 7, 9, 10, 12)] == [1, 1, 2, 2, 3, 3, 4, 4]
    assert jalali.add_months(1405, 1, -1) == (1404, 12)
    assert jalali.add_months(1405, 12, 1) == (1406, 1)
    assert jalali.add_months(1405, 7, -19) == (1403, 12)


def test_bucket_starts():
    day = D(2026, 10, 18)  # Sunday 1405/07/26
    assert jalali.week_start(day) == D(2026, 10, 17)  # Saturday
    assert jalali.week_start(D(2026, 10, 17)) == D(2026, 10, 17)
    assert jalali.week_start(D(2026, 10, 23)) == D(2026, 10, 17)  # Friday ends the week
    assert jalali.month_start(day) == D(2026, 9, 23)
    assert jalali.season_start(day) == D(2026, 9, 23)  # autumn = Mehr..Azar
    assert jalali.season_start(D(2026, 9, 22)) == D(2026, 6, 22)  # summer = Tir..Shahrivar
    assert jalali.season_start(D(2026, 3, 20)) == D(2025, 12, 22)  # winter 1404 = Dey..Esfand
    assert jalali.year_start(day) == D(2026, 3, 21)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-03-21", D(2026, 3, 21)),
        ("1405/01/01", D(2026, 3, 21)),
        ("1405-07-01", D(2026, 9, 23)),
        ("۱۴۰۵/۰۷/۲۶", D(2026, 10, 18)),
        (D(2020, 1, 2), D(2020, 1, 2)),
    ],
)
def test_parse_date(raw, expected):
    assert jalali.parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["", "yesterday", "1405/13/01", "2026-02-30", "26-1-1", 5])
def test_parse_date_rejects(raw):
    with pytest.raises(ValueError):
        jalali.parse_date(raw)


def test_bucket_labels():
    assert jalali.bucket_label(D(2026, 9, 23), "month", "jalali") == "1405-07"
    assert jalali.bucket_label(D(2026, 9, 23), "season", "jalali") == "1405-S3"
    assert jalali.bucket_label(D(2026, 3, 21), "year", "jalali") == "1405"
    assert jalali.bucket_label(D(2026, 10, 17), "week", "jalali") == "1405-07-25"
    assert jalali.bucket_label(D(2026, 9, 21), "week", "gregorian") == "2026-W39"
    assert jalali.bucket_label(D(2026, 10, 1), "season", "gregorian") == "2026-Q4"
    assert jalali.bucket_label(D(2026, 10, 1), "month", "gregorian") == "2026-10"


# --- calendar table ----------------------------------------------------------------


def test_day_row_attributes():
    row = table.day_row(D(2026, 3, 20))  # Friday, 1404/12/29
    assert (row["jalali_year"], row["jalali_month"], row["jalali_day"]) == (1404, 12, 29)
    assert row["jalali_season"] == 4
    assert row["jalali_month_start"] == D(2026, 2, 20)
    assert row["jalali_season_start"] == D(2025, 12, 22)
    assert row["jalali_year_start"] == D(2025, 3, 21)
    assert row["jalali_week_start"] == D(2026, 3, 14)
    assert row["jalali_weekday"] == 6  # Saturday=0 … Friday=6
    assert row["is_weekend"] and row["is_holiday"]
    assert row["holiday_name"] == "ملی شدن صنعت نفت"
    assert (row["year"], row["quarter"], row["month"], row["iso_week"]) == (2026, 1, 3, 12)
    assert row["week_start"] == D(2026, 3, 16) and row["quarter_start"] == D(2026, 1, 1)

    nowruz = table.day_row(D(2026, 3, 21))
    assert nowruz["is_holiday"] and nowruz["holiday_name"] == "نوروز"
    assert not nowruz["is_weekend"]  # a Saturday
    plain = table.day_row(D(2026, 10, 18))
    assert not plain["is_holiday"] and not plain["is_weekend"]


@pytest.mark.django_db
def test_populate_is_idempotent_and_keeps_edits():
    assert table.populate(D(2026, 3, 1), D(2026, 3, 31)) == 31
    CalendarDay.objects.filter(date=D(2026, 3, 10)).update(is_holiday=True, holiday_name="x")
    assert table.populate(D(2026, 2, 25), D(2026, 4, 2)) == 4 + 2
    assert CalendarDay.objects.count() == 37
    assert CalendarDay.objects.get(date=D(2026, 3, 10)).holiday_name == "x"
    assert table.missing_days(D(2026, 2, 20), D(2026, 4, 5)) == 5 + 3
    day = CalendarDay.objects.get(date=D(2026, 3, 21))
    assert str(day) == "1405/01/01" and not day.is_working_day


@pytest.mark.django_db
def test_every_jalali_month_is_contiguous():
    table.populate(D(2025, 3, 21), D(2026, 3, 20))  # all of 1404
    counts = {}
    for day in CalendarDay.objects.all():
        assert day.jalali_year == 1404
        counts[day.jalali_month] = counts.get(day.jalali_month, 0) + 1
        assert day.jalali_month_start == jalali.to_gregorian(1404, day.jalali_month, 1)
    assert counts == {m: jalali.month_length(1404, m) for m in range(1, 13)}
    assert sum(counts.values()) == 365


@pytest.mark.django_db
def test_gozar_calendar_command():
    out = StringIO()
    call_command("gozar_calendar", start="1405/01/01", end="1405/01/31", stdout=out)
    assert "31 days added" in out.getvalue()
    assert CalendarDay.objects.count() == 31
    call_command("gozar_calendar", start="2026-03-21", end="2026-04-20", stdout=out)
    assert CalendarDay.objects.count() == 31
    call_command("gozar_calendar", start="2026-03-21", end="2026-03-22", clear=True, stdout=out)
    assert CalendarDay.objects.count() == 31
    with pytest.raises(CommandError):
        call_command("gozar_calendar", start="1405/02/01", end="1405/01/01", stdout=out)
    with pytest.raises(CommandError):
        call_command("gozar_calendar", start="nope", stdout=out)


def test_default_range_spans_whole_jalali_years():
    start, end = table.default_range(D(2026, 10, 18))
    assert start == jalali.to_gregorian(1390, 1, 1)
    assert end == jalali.to_gregorian(1411, 1, 1) - dt.timedelta(days=1)


# --- relative periods (today frozen at 2026-10-18 = 1405/07/26, a Sunday) ----------

TODAY = D(2026, 10, 18)

JALALI_PERIODS = [
    ("today", D(2026, 10, 18), D(2026, 10, 18)),
    ("yesterday", D(2026, 10, 17), D(2026, 10, 17)),
    ("this_week", D(2026, 10, 17), TODAY),  # weeks start Saturday
    ("last_week", D(2026, 10, 10), D(2026, 10, 16)),
    ("this_month", D(2026, 9, 23), TODAY),  # 1405/07/01 →
    ("last_month", D(2026, 8, 23), D(2026, 9, 22)),  # Shahrivar 1405 (31 days)
    ("last_3_months", D(2026, 6, 22), D(2026, 9, 22)),  # Tir, Mordad, Shahrivar
    ("past_3_months", D(2026, 7, 23), TODAY),  # Mordad 1 → today
    ("this_season", D(2026, 9, 23), TODAY),  # autumn 1405
    ("last_season", D(2026, 6, 22), D(2026, 9, 22)),  # summer 1405
    ("last_2_seasons", D(2026, 3, 21), D(2026, 9, 22)),  # spring + summer
    ("last_quarter", D(2026, 6, 22), D(2026, 9, 22)),  # alias of season
    ("this_year", D(2026, 3, 21), TODAY),
    ("last_year", D(2025, 3, 21), D(2026, 3, 20)),  # 1404 (365 days)
    ("last_2_years", D(2024, 3, 20), D(2026, 3, 20)),  # 1403 (leap) + 1404
    ("last_7_days", D(2026, 10, 11), D(2026, 10, 17)),
    ("past_7_days", D(2026, 10, 12), TODAY),
    ("past_30_days", D(2026, 9, 19), TODAY),
    ("last_12_months", D(2025, 9, 23), D(2026, 9, 22)),  # 1404/07/01 → 1405/06/31
]


@pytest.mark.parametrize("name, start, end", JALALI_PERIODS)
def test_jalali_relative_periods(name, start, end):
    p = periods.resolve(name, "jalali", TODAY)
    assert (p.start, p.end) == (start, end)


GREGORIAN_PERIODS = [
    ("this_week", D(2026, 10, 12), TODAY),  # ISO weeks start Monday
    ("last_week", D(2026, 10, 5), D(2026, 10, 11)),
    ("this_month", D(2026, 10, 1), TODAY),
    ("last_month", D(2026, 9, 1), D(2026, 9, 30)),
    ("last_3_months", D(2026, 7, 1), D(2026, 9, 30)),
    ("this_season", D(2026, 10, 1), TODAY),  # calendar quarter
    ("last_season", D(2026, 7, 1), D(2026, 9, 30)),
    ("this_year", D(2026, 1, 1), TODAY),
    ("last_year", D(2025, 1, 1), D(2025, 12, 31)),
    ("last_14_months", D(2025, 8, 1), D(2026, 9, 30)),
]


@pytest.mark.parametrize("name, start, end", GREGORIAN_PERIODS)
def test_gregorian_relative_periods(name, start, end):
    p = periods.resolve(name, "gregorian", TODAY)
    assert (p.start, p.end) == (start, end)


def test_year_boundary_rollover():
    nowruz = D(2026, 3, 21)  # first day of 1405
    assert periods.resolve("last_month", "jalali", nowruz).start == D(2026, 2, 20)
    assert periods.resolve("last_month", "jalali", nowruz).end == D(2026, 3, 20)
    assert periods.resolve("this_year", "jalali", nowruz).start == nowruz
    assert periods.resolve("this_week", "jalali", nowruz).start == nowruz  # Saturday
    jan1 = D(2026, 1, 1)
    p = periods.resolve("last_season", "gregorian", jan1)
    assert (p.start, p.end) == (D(2025, 10, 1), D(2025, 12, 31))


def test_resolve_uses_frozen_today(frozen_today):
    p = periods.resolve("this_month")  # Gregorian by default
    assert (p.start, p.end, p.calendar) == (D(2026, 10, 1), frozen_today, "gregorian")
    assert p.as_dict() == {
        "name": "this_month",
        "calendar": "gregorian",
        "start": "2026-10-01",
        "end": "2026-10-18",
    }
    p = periods.resolve("this_month", "jalali")
    assert (p.start, p.end, p.calendar) == (D(2026, 9, 23), frozen_today, "jalali")
    assert p.as_dict() == {
        "name": "this_month",
        "calendar": "jalali",
        "start": "2026-09-23",
        "end": "2026-10-18",
        "start_jalali": "1405-07-01",
        "end_jalali": "1405-07-26",
    }


@pytest.mark.parametrize(
    "name",
    [
        "",
        "last",
        "forever",
        "this_3_months",
        "past_month",
        "last_0_days",
        "last_1001_days",
        "last_3_fortnights",
        "next_month",
        "last-month",
    ],
)
def test_invalid_periods(name):
    assert not periods.is_valid(name)
    with pytest.raises(periods.PeriodError):
        periods.resolve(name, "jalali", TODAY)


def test_period_names_are_case_and_plural_insensitive():
    assert periods.parse("Last_3_Months") == ("last", 3, "month")
    assert periods.parse("last_1_month") == ("last", 1, "month")
    assert periods.parse("last_days") == ("last", 1, "day")
    assert all(periods.is_valid(name) for name in periods.EXAMPLES)
