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
from django.utils.translation import gettext as _

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
        raise PeriodError(_("Unknown relative period %(name)r") % {"name": name})
    kind, raw_count, unit = m.groups()
    unit = {"quarter": "season"}.get(unit, unit)
    if unit not in UNITS:
        raise PeriodError(
            _("Unknown unit %(unit)r in relative period %(name)r") % {"unit": unit, "name": name}
        )
    if kind == "this" and raw_count is not None:
        raise PeriodError(_("'this' takes no count: %(name)r") % {"name": name})
    if kind == "past" and raw_count is None:
        raise PeriodError(_("'past' needs a count, e.g. past_3_%(unit)ss") % {"unit": unit})
    count = int(raw_count) if raw_count is not None else 1
    if not 1 <= count <= MAX_COUNT:
        raise PeriodError(
            _("Count must be between 1 and %(max)s: %(name)r") % {"max": MAX_COUNT, "name": name}
        )
    return kind, count, unit


def resolve(name: str, calendar: Calendar = "gregorian", on: dt.date | None = None) -> Period:
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


# --- comparison periods (ReportSpec.compare_to) ------------------------------------

CompareKind = Literal["previous_period", "same_period_last_year"]
COMPARE_KINDS: tuple[str, ...] = ("previous_period", "same_period_last_year")
_UNIT_MONTHS = {"month": 1, "season": 3, "year": 12}


def is_month_start(day: dt.date, calendar: Calendar) -> bool:
    return unit_start(day, "month", calendar) == day


def add_months(day: dt.date, months: int, calendar: Calendar) -> dt.date:
    """``day`` moved by ``months`` in ``calendar``; the day of month is clamped to the
    target month's length (31 Mordad − 1 month = 31 Tir; 31 Mehr − 1 = 30 Shahrivar;
    30 Esfand of a leap year + 12 months = 29 Esfand)."""
    if calendar == "jalali":
        j = jalali.to_jalali(day)
        y, m = jalali.add_months(j.year, j.month, months)
        return jalali.to_gregorian(y, m, min(j.day, jalali.month_length(y, m)))
    y, m = jalali.add_months(day.year, day.month, months)
    last = (dt.date(*jalali.add_months(y, m, 1), 1) - dt.timedelta(days=1)).day
    return dt.date(y, m, min(day.day, last))


def months_between(start: dt.date, end: dt.date, calendar: Calendar) -> int:
    """Whole months from month start ``start`` to month start ``end``."""
    if calendar == "jalali":
        a, b = jalali.to_jalali(start), jalali.to_jalali(end)
    else:
        a, b = start, end
    return (b.year - a.year) * 12 + (b.month - a.month)


@dataclass(frozen=True)
class Offset:
    """A shift in ``calendar``: whole months (day clamped to the month) or days."""

    months: int = 0
    days: int = 0
    calendar: Calendar = "gregorian"

    def apply(self, day: dt.date) -> dt.date:
        if self.months:
            return add_months(day, self.months, self.calendar)
        return day + dt.timedelta(days=self.days)

    def inverse(self) -> Offset:
        return Offset(-self.months, -self.days, self.calendar)

    def shift_range(self, start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
        """The range moved by this offset; a range ending on a month boundary keeps
        ending on one (Shahrivar 1–31 − 1 month = Mordad 1–31, not 1–30)."""
        after = end + dt.timedelta(days=1)
        if self.months and is_month_start(after, self.calendar):
            return self.apply(start), self.apply(after) - dt.timedelta(days=1)
        return self.apply(start), self.apply(end)


@dataclass(frozen=True)
class Comparison:
    """The period a report is compared with, and how its time buckets line up with
    the report's own: each comparison bucket maps to exactly one current bucket."""

    kind: str
    start: dt.date
    end: dt.date
    calendar: Calendar
    offset: Offset  # current period → comparison period
    current_start: dt.date

    def as_dict(self) -> dict:
        out = {
            "kind": self.kind,
            "calendar": self.calendar,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }
        if self.calendar == "jalali":
            out["start_jalali"] = jalali.bucket_label(self.start, "day", "jalali")
            out["end_jalali"] = jalali.bucket_label(self.end, "day", "jalali")
        return out

    def _week_days(self) -> int:
        """A month-based offset seen by weekly buckets: the period shift rounded to
        whole weeks, so week starts map onto week starts one-to-one."""
        delta = (self.start - self.current_start).days
        return round(delta / 7) * 7

    def to_current(self, bucket: dt.date, grain: str) -> tuple[dt.date, bool]:
        """The current bucket a comparison bucket lines up with, and whether the
        match is exact (``False`` when a day was clamped, e.g. 29 Feb → 28 Feb)."""
        back = self.offset.inverse()
        if grain == "week" and self.offset.months:
            moved = bucket - dt.timedelta(days=self._week_days())
            return unit_start(moved, "week", self.calendar), True
        moved = back.apply(bucket)
        exact = not back.months or self.offset.apply(moved) == bucket
        return unit_start(moved, grain, self.calendar), exact

    def to_comparison(self, bucket: dt.date, grain: str) -> dt.date | None:
        """The comparison bucket shown next to current ``bucket`` (for drill-down)."""
        if grain == "week" and self.offset.months:
            guess = bucket + dt.timedelta(days=self._week_days())
        else:
            guess = self.offset.apply(bucket)
        first = unit_start(guess, grain, self.calendar)
        candidates = [first, shift(first, grain, -1, self.calendar)]
        candidates.append(shift(first, grain, 1, self.calendar))
        exact = [c for c in candidates if self.to_current(c, grain) == (bucket, True)]
        loose = [c for c in candidates if self.to_current(c, grain)[0] == bucket]
        return (exact or loose or [None])[0]


def comparison(
    kind: str,
    start: dt.date,
    end: dt.date,
    calendar: Calendar = "gregorian",
    name: str | None = None,
) -> Comparison:
    """The comparison period for the inclusive range ``start``–``end``.

    ``previous_period``: the same span immediately before. A relative period moves
    by its own length in its own units (``last_3_months`` → the 3 months before;
    ``this_month`` to date → the previous month up to the same day); a custom range
    of whole months moves by that many months, any other range by its length in days.

    ``same_period_last_year``: one year earlier in ``calendar`` (Jalali or Gregorian
    months, the day clamped to the month's length); weekly periods move 52 weeks so
    weekdays line up."""
    if kind not in COMPARE_KINDS:
        raise PeriodError(_("Unknown comparison %(name)r") % {"name": kind})
    unit, count = None, 1
    if name is not None:
        _kind, count, unit = parse(name)
    if kind == "same_period_last_year":
        offset = Offset(days=-364) if unit == "week" else Offset(months=-12, calendar=calendar)
    elif unit is not None:
        if unit in _UNIT_MONTHS:
            offset = Offset(months=-count * _UNIT_MONTHS[unit], calendar=calendar)
        else:
            offset = Offset(days=-count * (7 if unit == "week" else 1))
    else:
        after = end + dt.timedelta(days=1)
        if is_month_start(start, calendar) and is_month_start(after, calendar):
            offset = Offset(months=-months_between(start, after, calendar), calendar=calendar)
        else:
            offset = Offset(days=-((end - start).days + 1))
    if offset.months == 0 and offset.days == 0:  # a zero-length span can't happen, but
        offset = Offset(days=-1)  # never compare a period with itself
    c_start, c_end = offset.shift_range(start, end)
    return Comparison(kind, c_start, c_end, calendar, offset, start)
