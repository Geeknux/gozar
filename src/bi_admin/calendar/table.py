"""Building and checking the ``CalendarDay`` dimension table."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

from django.conf import settings

from bi_admin.calendar import jalali

# Python weekday numbers (Monday=0); Friday is the Iranian weekend.
DEFAULT_WEEKEND_DAYS = (4,)
# Jalali years covered when ``gozar_calendar`` runs without a range.
DEFAULT_YEARS_BACK = 15
DEFAULT_YEARS_AHEAD = 5


def weekend_days() -> tuple[int, ...]:
    return tuple((getattr(settings, "GOZAR", {}) or {}).get("WEEKEND_DAYS", DEFAULT_WEEKEND_DAYS))


def default_range(today: dt.date | None = None) -> tuple[dt.date, dt.date]:
    year = jalali.to_jalali(today or dt.date.today()).year
    start = jalali.to_gregorian(year - DEFAULT_YEARS_BACK, 1, 1)
    end = jalali.to_gregorian(year + DEFAULT_YEARS_AHEAD + 1, 1, 1) - dt.timedelta(days=1)
    return start, end


def day_row(day: dt.date, weekend: tuple[int, ...] = DEFAULT_WEEKEND_DAYS) -> dict:
    j = jalali.to_jalali(day)
    iso = day.isocalendar()
    quarter = (day.month - 1) // 3 + 1
    season = jalali.season_of(j.month)
    holiday = jalali.FIXED_HOLIDAYS.get((j.month, j.day), "")
    return {
        "date": day,
        "weekday": day.weekday(),
        "year": day.year,
        "quarter": quarter,
        "month": day.month,
        "day": day.day,
        "iso_year": iso.year,
        "iso_week": iso.week,
        "week_start": day - dt.timedelta(days=day.weekday()),
        "month_start": day.replace(day=1),
        "quarter_start": dt.date(day.year, (quarter - 1) * 3 + 1, 1),
        "year_start": dt.date(day.year, 1, 1),
        "jalali_year": j.year,
        "jalali_season": season,
        "jalali_month": j.month,
        "jalali_day": j.day,
        "jalali_weekday": (day.weekday() + 2) % 7,
        "jalali_week_start": jalali.week_start(day),
        "jalali_month_start": jalali.to_gregorian(j.year, j.month, 1),
        "jalali_season_start": jalali.to_gregorian(j.year, (season - 1) * 3 + 1, 1),
        "jalali_year_start": jalali.to_gregorian(j.year, 1, 1),
        "is_weekend": day.weekday() in weekend,
        "is_holiday": bool(holiday),
        "holiday_name": holiday,
    }


def iter_days(start: dt.date, end: dt.date) -> Iterator[dt.date]:
    day = start
    while day <= end:
        yield day
        day += dt.timedelta(days=1)


def populate(start: dt.date, end: dt.date, batch_size: int = 1000) -> int:
    """Insert the missing days in ``[start, end]``; existing rows are kept as they
    are (so manually added holidays survive). Returns the number of rows created."""
    from bi_admin.models import CalendarDay

    if end < start:
        raise ValueError("end is before start")
    existing = set(
        CalendarDay.objects.filter(date__range=(start, end)).values_list("date", flat=True)
    )
    weekend = weekend_days()
    rows = [CalendarDay(**day_row(d, weekend)) for d in iter_days(start, end) if d not in existing]
    CalendarDay.objects.bulk_create(rows, batch_size=batch_size)
    return len(rows)


def missing_days(start: dt.date, end: dt.date) -> int:
    """How many days of ``[start, end]`` the table lacks."""
    from bi_admin.models import CalendarDay

    have = CalendarDay.objects.filter(date__range=(start, end)).count()
    return (end - start).days + 1 - have
