"""Relative periods ("last_3_months", "this_season", …) resolved in plain Python.

The NL interface and the report builder only ever send a period *name*; the
concrete date range is computed here, deterministically, against today's date
in the active time zone — never by an LLM.

Grammar (``unit`` ∈ day, week, month, season, quarter, year; plural accepted)::

    today | yesterday
    this_<unit>          current unit, from its first day up to today
    last_<unit>          the previous complete unit
    last_<N>_<unit>s     the N complete units before the current one
    past_<N>_<unit>s     the current unit (to date) plus the N-1 before it

Weeks start on Saturday in the Jalali calendar and on Monday (ISO) in the
Gregorian one; ``season`` is a Jalali season (3 months from Farvardin) or a
Gregorian quarter, and ``quarter`` is an alias of ``season``.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Literal

from django.utils import timezone

from bi_admin.calendar import jalali

Calendar = Literal["jalali", "gregorian"]
UNITS = ("day", "week", "month", "season", "year")
MAX_COUNT = 1000
EXAMPLES = (
    "today",
    "yesterday",
    "this_week",
    "this_month",
    "this_season",
    "this_year",
    "last_week",
    "last_month",
    "last_season",
    "last_year",
    "last_7_days",
    "last_3_months",
    "last_2_seasons",
    "past_30_days",
    "past_6_months",
)

_RE = re.compile(r"^(?:(this|last|past)_(?:(\d+)_)?([a-z]+?)s?)$")


class PeriodError(ValueError):
    pass


@dataclass(frozen=True)
class Period:
    name: str
    start: dt.date  # inclusive
    end: dt.date  # inclusive
    calendar: Calendar

    def as_dict(self) -> dict:
        out = {
            "name": self.name,
            "calendar": self.calendar,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }
        if self.calendar == "jalali":
            out["start_jalali"] = jalali.bucket_label(self.start, "day", "jalali")
            out["end_jalali"] = jalali.bucket_label(self.end, "day", "jalali")
        return out


def today() -> dt.date:
    """Today in the active time zone. Tests monkeypatch this to freeze time."""
    return timezone.localdate()


def unit_start(day: dt.date, unit: str, calendar: Calendar) -> dt.date:
    if unit == "day":
        return day
    if calendar == "jalali":
        return {
            "week": jalali.week_start,
            "month": jalali.month_start,
            "season": jalali.season_start,
            "year": jalali.year_start,
        }[unit](day)
    if unit == "week":
        return day - dt.timedelta(days=day.weekday())
    if unit == "month":
        return day.replace(day=1)
    if unit == "season":
        return dt.date(day.year, (day.month - 1) // 3 * 3 + 1, 1)
    return dt.date(day.year, 1, 1)


def shift(start: dt.date, unit: str, count: int, calendar: Calendar) -> dt.date:
    """Move a unit-aligned ``start`` by ``count`` units (negative = back)."""
    if unit == "day":
        return start + dt.timedelta(days=count)
    if unit == "week":
        return start + dt.timedelta(weeks=count)
    months = {"month": 1, "season": 3, "year": 12}[unit] * count
    if calendar == "jalali":
        j = jalali.to_jalali(start)
        y, m = jalali.add_months(j.year, j.month, months)
        return jalali.to_gregorian(y, m, 1)
    y, m = jalali.add_months(start.year, start.month, months)  # same index maths
    return dt.date(y, m, 1)


def parse(name: str) -> tuple[str, int, str]:
    """Split a period name into ``(kind, count, unit)``; raises ``PeriodError``."""
    key = name.strip().lower()
    aliases = {"today": "this_day", "yesterday": "last_day"}
    key = aliases.get(key, key)
    m = _RE.match(key)
    if not m:
        raise PeriodError(f"Unknown relative period {name!r}")
    kind, raw_count, unit = m.groups()
    unit = {"quarter": "season"}.get(unit, unit)
    if unit not in UNITS:
        raise PeriodError(f"Unknown unit {unit!r} in relative period {name!r}")
    if kind == "this" and raw_count is not None:
        raise PeriodError(f"'this' takes no count: {name!r}")
    if kind == "past" and raw_count is None:
        raise PeriodError(f"'past' needs a count, e.g. past_3_{unit}s")
    count = int(raw_count) if raw_count is not None else 1
    if not 1 <= count <= MAX_COUNT:
        raise PeriodError(f"Count must be between 1 and {MAX_COUNT}: {name!r}")
    return kind, count, unit


def resolve(name: str, calendar: Calendar = "jalali", on: dt.date | None = None) -> Period:
    """Resolve ``name`` to an inclusive date range relative to ``on`` (default today)."""
    kind, count, unit = parse(name)
    ref = on or today()
    current = unit_start(ref, unit, calendar)
    if kind == "last":
        start = shift(current, unit, -count, calendar)
        end = current - dt.timedelta(days=1)
    else:  # this / past: up to and including today
        start = shift(current, unit, -(count - 1), calendar)
        end = ref
    return Period(name=name, start=start, end=end, calendar=calendar)


def is_valid(name: str) -> bool:
    try:
        parse(name)
    except PeriodError:
        return False
    return True
