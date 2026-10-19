"""Deterministic date helpers for the NL layer: whole years/months written as
``1404``, ``1404/03``, ``2025`` or ``2025-03`` expand to their first/last day.

The LLM is told to copy dates as written; expansion (which needs month lengths
and Jalali leap years) happens here, never in the model.
"""

from __future__ import annotations

import datetime as dt
import re

from bi_admin.calendar import jalali

_PARTIAL = re.compile(r"^\s*(\d{4})(?:[-/](\d{1,2}))?\s*$")


def _bounds(year: int, month: int | None) -> tuple[dt.date, dt.date]:
    if year < jalali.JALALI_YEAR_CUTOFF:
        if month is None:
            start = jalali.to_gregorian(year, 1, 1)
            end = jalali.to_gregorian(year + 1, 1, 1) - dt.timedelta(days=1)
        else:
            start = jalali.to_gregorian(year, month, 1)
            end = start + dt.timedelta(days=jalali.month_length(year, month) - 1)
        return start, end
    if month is None:
        return dt.date(year, 1, 1), dt.date(year, 12, 31)
    start = dt.date(year, month, 1)
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return start, nxt - dt.timedelta(days=1)


def partial_bounds(value: object) -> tuple[dt.date, dt.date] | None:
    """``(first, last)`` day of a year/month written as ``YYYY`` or ``YYYY/MM``."""
    if not isinstance(value, str | int) or isinstance(value, bool):
        return None
    m = _PARTIAL.match(str(value).translate(jalali.PERSIAN_DIGITS))
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2)) if m.group(2) else None
    if month is not None and not 1 <= month <= 12:
        return None
    try:
        return _bounds(year, month)
    except ValueError:
        return None


def iso(value):
    """A full date (Gregorian or Jalali, any digits) as an ISO date; else unchanged."""
    if not isinstance(value, str):
        return value
    try:
        return jalali.parse_date(value).isoformat()
    except ValueError:
        return value


def expand_filter(op: str, value):
    """``(op, value)`` on a time dimension with dates as ISO Gregorian dates and whole
    years/months (``1404``, ``1404/03``) expanded to their first/last day."""
    if op == "between" and isinstance(value, list) and len(value) == 2:
        lo, hi = partial_bounds(value[0]), partial_bounds(value[1])
        return op, [
            lo[0].isoformat() if lo else iso(value[0]),
            hi[1].isoformat() if hi else iso(value[1]),
        ]
    if isinstance(value, list):
        return op, [iso(v) for v in value]
    bounds = partial_bounds(value)
    if bounds is None:
        return op, iso(value)
    if op == "eq":
        return "between", [bounds[0].isoformat(), bounds[1].isoformat()]
    if op == "gte":
        return op, bounds[0].isoformat()
    if op == "lte":
        return op, bounds[1].isoformat()
    return op, value


def most_recent_month(month: int, today: dt.date, calendar: str) -> str:
    """``YYYY/MM`` (Jalali) or ``YYYY-MM`` of the latest ``month`` that has started."""
    if calendar == "jalali":
        j = jalali.to_jalali(today)
        year = j.year if month <= j.month else j.year - 1
        return f"{year}/{month:02d}"
    year = today.year if month <= today.month else today.year - 1
    return f"{year}-{month:02d}"
