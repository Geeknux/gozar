"""``date_hierarchy``: drill down a list by year → month → day, in the user's calendar.

The list takes ``dh_year``, ``dh_month``, ``dh_day`` (numbers in ``dh_calendar``,
default the user's calendar), turns them into a half-open local-date range on
the hierarchy field and reports the next level's buckets with their row counts
(one conditional-aggregate query). Jalali years and months don't line up with
Gregorian ones, so the ranges come from ``bi_admin.calendar.jalali``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.db import models
from django.db.models import Count, Max, Min, Q
from django.utils import timezone
from django.utils.translation import gettext as _

from bi_admin import preferences
from bi_admin.calendar import jalali
from bi_admin.discovery import get_registry

MAX_YEARS = 200


def _calendar(request, params) -> str:
    cal = params.get("dh_calendar") or preferences.get(request).calendar
    return cal if cal in ("gregorian", "jalali") else "gregorian"


def _selected(params) -> tuple[int | None, int | None, int | None]:
    from bi_admin.crud.service import CrudError

    out = []
    for key in ("dh_year", "dh_month", "dh_day"):
        raw = params.get(key)
        if raw in (None, ""):
            out.append(None)
            continue
        try:
            out.append(int(raw))
        except ValueError:
            raise CrudError(400, _("Invalid number %(value)r") % {"value": raw}) from None
    year, month, day = out
    if (month and not year) or (day and not month):
        raise CrudError(400, _("Select the year (and month) first"))
    return year, month, day


def _range(cal: str, year: int, month: int | None, day: int | None) -> tuple[dt.date, dt.date]:
    from bi_admin.crud.service import CrudError

    try:
        if cal == "jalali":
            if day:
                start = jalali.to_gregorian(year, month, day)
                return start, start + dt.timedelta(days=1)
            if month:
                ny, nm = jalali.add_months(year, month, 1)
                return jalali.to_gregorian(year, month, 1), jalali.to_gregorian(ny, nm, 1)
            return jalali.to_gregorian(year, 1, 1), jalali.to_gregorian(year + 1, 1, 1)
        if day:
            start = dt.date(year, month, day)
            return start, start + dt.timedelta(days=1)
        if month:
            nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
            return dt.date(year, month, 1), nxt
        return dt.date(year, 1, 1), dt.date(year + 1, 1, 1)
    except (ValueError, OverflowError):
        raise CrudError(400, _("Invalid date")) from None


def _bound(kind: str, day: dt.date):
    if kind == "date":
        return day
    return timezone.make_aware(dt.datetime.combine(day, dt.time.min))


def _q(path: str, kind: str, start: dt.date, end: dt.date) -> Q:
    return Q(**{f"{path}__gte": _bound(kind, start), f"{path}__lt": _bound(kind, end)})


def _kind(options) -> str:
    _steps, finfo = get_registry().resolve_lookup(options.label, options.date_hierarchy)
    return finfo.kind


def apply(options, request, qs: models.QuerySet, params) -> models.QuerySet:
    """``qs`` narrowed to the selected year/month/day (unchanged without one)."""
    if not options.date_hierarchy:
        return qs
    year, month, day = _selected(params)
    if year is None:
        return qs
    start, end = _range(_calendar(request, params), year, month, day)
    return qs.filter(_q(options.date_hierarchy, _kind(options), start, end))


def _local(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return value


def describe(options, request, qs: models.QuerySet, params) -> dict[str, Any] | None:
    """The hierarchy's state and the buckets of the next level, for the list response."""
    from bi_admin.crud.service import path_label

    path = options.date_hierarchy
    if not path:
        return None
    cal = _calendar(request, params)
    year, month, day = _selected(params)
    kind = _kind(options)
    level = "year" if year is None else "month" if month is None else "day" if day is None else ""
    out: dict[str, Any] = {
        "field": path,
        "label": path_label(options.model, path),
        "calendar": cal,
        "selected": {"year": year, "month": month, "day": day},
        "level": level or None,
        "buckets": [],
    }
    if not level:
        return out
    base = qs.order_by()
    bounds = base.aggregate(low=Min(path), high=Max(path))
    if bounds["low"] is None:
        return out
    low, high = _local(bounds["low"]), _local(bounds["high"])
    ranges = _candidates(cal, level, low, high, year, month)
    if not ranges:
        return out
    pk = "pk"
    counts = base.aggregate(
        **{
            f"b{i}": Count(pk, filter=_q(path, kind, start, end), distinct=base.query.distinct)
            for i, (_parts, start, end) in enumerate(ranges)
        }
    )
    for i, (parts, start, _end) in enumerate(ranges):
        n = counts[f"b{i}"]
        if n:
            out["buckets"].append({**parts, "start": start.isoformat(), "count": n})
    return out


def _candidates(cal, level, low: dt.date, high: dt.date, year, month):
    """``[(parts, start, end), ...]`` for every bucket of ``level`` between the
    data's first and last date (inside the selected year/month)."""
    out = []
    if level == "year":
        if cal == "jalali":
            first, last = jalali.to_jalali(low).year, jalali.to_jalali(high).year
        else:
            first, last = low.year, high.year
        for y in range(max(first, last - MAX_YEARS), last + 1):
            out.append(({"year": y}, *_range(cal, y, None, None)))
    elif level == "month":
        for m in range(1, 13):
            out.append(({"year": year, "month": m}, *_range(cal, year, m, None)))
    elif level == "day":
        days = jalali.month_length(year, month) if cal == "jalali" else _days_in(year, month)
        for d in range(1, days + 1):
            out.append(({"year": year, "month": month, "day": d}, *_range(cal, year, month, d)))
    return [(p, s, e) for p, s, e in out if e > low and s <= high]


def _days_in(year: int, month: int) -> int:
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return (nxt - dt.timedelta(days=1)).day
