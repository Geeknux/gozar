"""Pure-Python Jalali (Solar Hijri) helpers on top of ``jdatetime``.

Everything here is deterministic date arithmetic used by the calendar table,
relative periods and bucket labels. Grouping itself happens in the database
(see ``bi_admin.models.CalendarDay``); these helpers never aggregate data.
"""

from __future__ import annotations

import datetime as dt
import re

import jdatetime

SEASON_NAMES = {
    "fa": {1: "بهار", 2: "تابستان", 3: "پاییز", 4: "زمستان"},
    "en": {1: "Spring", 2: "Summer", 3: "Autumn", 4: "Winter"},
}
MONTH_NAMES_FA = [
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
]

# Official holidays that fall on fixed Jalali dates (month, day). Lunar (Hijri
# qamari) holidays move every year and are not included; set them per row.
FIXED_HOLIDAYS = {
    (1, 1): "نوروز",
    (1, 2): "نوروز",
    (1, 3): "نوروز",
    (1, 4): "نوروز",
    (1, 12): "روز جمهوری اسلامی",
    (1, 13): "روز طبیعت",
    (3, 14): "رحلت امام خمینی",
    (3, 15): "قیام ۱۵ خرداد",
    (11, 22): "پیروزی انقلاب اسلامی",
    (12, 29): "ملی شدن صنعت نفت",
}

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_DATE_RE = re.compile(r"^\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*$")
# Gregorian years below this are read as Jalali (1300–1499 ↔ 1921–2121).
JALALI_YEAR_CUTOFF = 1700


def to_jalali(day: dt.date) -> jdatetime.date:
    return jdatetime.date.fromgregorian(date=day)


def to_gregorian(year: int, month: int, day: int = 1) -> dt.date:
    return jdatetime.date(year, month, day).togregorian()


def month_length(year: int, month: int) -> int:
    if month < 12:
        return 31 if month <= 6 else 30
    return 30 if jdatetime.date(year, 1, 1).isleap() else 29


def season_of(month: int) -> int:
    return (month - 1) // 3 + 1


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def month_start(day: dt.date) -> dt.date:
    j = to_jalali(day)
    return to_gregorian(j.year, j.month, 1)


def season_start(day: dt.date) -> dt.date:
    j = to_jalali(day)
    return to_gregorian(j.year, (season_of(j.month) - 1) * 3 + 1, 1)


def year_start(day: dt.date) -> dt.date:
    return to_gregorian(to_jalali(day).year, 1, 1)


def week_start(day: dt.date) -> dt.date:
    """Iranian weeks start on Saturday (``weekday() == 5``)."""
    return day - dt.timedelta(days=(day.weekday() + 2) % 7)


def parse_date(value: str | dt.date) -> dt.date:
    """Parse ``YYYY-MM-DD`` (Gregorian) or ``YYYY/MM/DD`` / ``YYYY-MM-DD`` with a
    Jalali year (< 1700). Persian/Arabic digits are accepted."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"Not a date: {value!r}")
    m = _DATE_RE.match(value.translate(PERSIAN_DIGITS))
    if not m:
        raise ValueError(f"Not a date: {value!r}")
    y, mo, d = (int(g) for g in m.groups())
    if y < JALALI_YEAR_CUTOFF:
        return to_gregorian(y, mo, d)
    return dt.date(y, mo, d)


def bucket_label(start: dt.date, grain: str, calendar: str) -> str:
    """Stable, language-neutral label for a time bucket that starts at ``start``.

    Jalali: ``1405-07-26`` (day/week start), ``1405-07``, ``1405-S3``, ``1405``.
    Gregorian: ``2026-10-18``, ``2026-W42``, ``2026-10``, ``2026-Q4``, ``2026``.
    """
    if calendar == "jalali":
        j = to_jalali(start)
        if grain in ("day", "week"):
            return f"{j.year:04d}-{j.month:02d}-{j.day:02d}"
        if grain == "month":
            return f"{j.year:04d}-{j.month:02d}"
        if grain == "season":
            return f"{j.year:04d}-S{season_of(j.month)}"
        return f"{j.year:04d}"
    if grain == "day":
        return start.isoformat()
    if grain == "week":
        iso = start.isocalendar()
        return f"{iso.year:04d}-W{iso.week:02d}"
    if grain == "month":
        return f"{start.year:04d}-{start.month:02d}"
    if grain == "season":
        return f"{start.year:04d}-Q{(start.month - 1) // 3 + 1}"
    return f"{start.year:04d}"
