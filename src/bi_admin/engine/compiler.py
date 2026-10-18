"""Compile a ``ValidatedReport`` into a Django ORM queryset.

The ORM does all the work — filters become ``Q`` objects, dimensions become
``values()`` group keys, measures become aggregate annotations — so managers,
the semantic model's ``get_queryset`` and (from M4) row-level rules always apply,
and no user/LLM input ever reaches SQL as text.

Time grains:

* Gregorian: ``Trunc*`` in the active time zone (weeks start Monday; ``season``
  is a calendar quarter).
* Jalali: a correlated lookup into the ``CalendarDay`` table by local date,
  returning the Gregorian date the Jalali week/month/season/year starts on. The
  grouping therefore happens in the database on real Jalali boundaries.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from django.db import models
from django.db.models import F, OuterRef, Q, Subquery
from django.db.models.functions import Trunc, TruncDate
from django.utils import timezone

from bi_admin.engine.validator import GroupKey, ResolvedFilter, ValidatedReport
from bi_admin.models import CalendarDay

GREGORIAN_TRUNC = {"week": "week", "month": "month", "season": "quarter", "year": "year"}
JALALI_COLUMN = {
    "day": "date",
    "week": "jalali_week_start",
    "month": "jalali_month_start",
    "season": "jalali_season_start",
    "year": "jalali_year_start",
}


@dataclass
class CompiledReport:
    queryset: models.QuerySet
    aliases: dict[str, str]  # output key → queryset alias
    aggregate: bool  # True: no group keys, run with .aggregate()
    report: ValidatedReport
    measures: dict[str, Any]  # alias → aggregate expression
    base: models.QuerySet  # filtered rows before grouping
    lookups: dict[str, str]  # dimension key → ORM lookup/alias

    @property
    def sql(self) -> str:
        return str(self.queryset.query)


def dim_alias(i: int) -> str:
    return f"_gd{i}"


def measure_alias(i: int) -> str:
    return f"_gm{i}"


def local_date(lookup, is_date: bool):
    """``lookup`` (a field path, alias or expression) as a date in the active time zone."""
    if is_date:
        return F(lookup) if isinstance(lookup, str) else lookup
    return TruncDate(lookup, tzinfo=timezone.get_current_timezone())


def bucket_expr(group: GroupKey, calendar: str, lookup: str):
    """Expression yielding the Gregorian start date of the bucket a row falls in."""
    dim, grain = group.dimension, group.grain
    if grain == "day":
        return local_date(lookup, dim.is_date)
    if calendar == "gregorian":
        return Trunc(
            lookup,
            GREGORIAN_TRUNC[grain],
            output_field=models.DateField(),
            tzinfo=None if dim.is_date else timezone.get_current_timezone(),
        )
    # ``lookup`` is the outer row's local date (see compile_report); the
    # calendar row for that day carries the Jalali bucket start.
    ref = OuterRef(lookup)
    days = CalendarDay.objects.filter(date=ref).order_by().values(JALALI_COLUMN[grain])[:1]
    return Subquery(days, output_field=models.DateField())


def day_bounds(day: dt.date, is_date: bool):
    """Start of ``day`` as an aware datetime (or the date itself for DateFields)."""
    if is_date:
        return day
    return timezone.make_aware(dt.datetime.combine(day, dt.time.min))


def date_range_q(lookup: str, start: dt.date | None, end: dt.date | None, is_date: bool) -> Q:
    """``start <= value <= end`` on local dates, as an index-friendly half-open range."""
    q = Q()
    if start is not None:
        q &= Q(**{f"{lookup}__gte": day_bounds(start, is_date)})
    if end is not None:
        if is_date:
            q &= Q(**{f"{lookup}__lte": end})
        else:
            q &= Q(**{f"{lookup}__lt": day_bounds(end + dt.timedelta(days=1), False)})
    return q


def filter_q(f: ResolvedFilter, lookup: str) -> Q:
    dim, op, value = f.dimension, f.op, f.value
    if op == "is_null":
        return Q(**{f"{lookup}__isnull": value})
    if dim.type == "time":
        if op == "eq":
            return date_range_q(lookup, value, value, dim.is_date)
        if op == "neq":
            return ~date_range_q(lookup, value, value, dim.is_date)
        if op == "gte":
            return date_range_q(lookup, value, None, dim.is_date)
        if op == "lte":
            return date_range_q(lookup, None, value, dim.is_date)
        if op == "between":
            return date_range_q(lookup, value[0], value[1], dim.is_date)
        days = Q()
        for day in value:  # in / not_in on dates
            days |= date_range_q(lookup, day, day, dim.is_date)
        return days if op == "in" else ~days
    simple = {"eq": "exact", "gte": "gte", "lte": "lte", "in": "in", "contains": "icontains"}
    if op in simple:
        return Q(**{f"{lookup}__{simple[op]}": value})
    if op == "neq":
        return ~Q(**{f"{lookup}__exact": value})
    if op == "not_in":
        return ~Q(**{f"{lookup}__in": value})
    if op == "between":
        return Q(**{f"{lookup}__range": tuple(value)})
    raise ValueError(f"Unsupported operator {op!r}")


def compile_report(report: ValidatedReport, request=None) -> CompiledReport:
    sem = report.semantic
    qs = sem.get_queryset(request)

    # Expression dimensions get an alias so filters and buckets can address them.
    used = {f.dimension.key for f in report.filters} | {g.key for g in report.groups}
    if report.period is not None and report.time_dimension is not None:
        used.add(report.time_dimension.key)
    lookups: dict[str, str] = {}
    for key in sorted(used):
        dim = sem.dimensions[key]
        if dim.path is None:
            lookups[key] = f"_gx_{key}"
            qs = qs.alias(**{lookups[key]: dim.expr})
        else:
            lookups[key] = dim.path

    q = Q()
    for f in report.filters:
        q &= filter_q(f, lookups[f.dimension.key])
    if report.period is not None and report.time_dimension is not None:
        td = report.time_dimension
        q &= date_range_q(lookups[td.key], report.period.start, report.period.end, td.is_date)
    if q:
        qs = qs.filter(q)

    aliases: dict[str, str] = {}
    group_exprs = {}
    for i, group in enumerate(report.groups):
        alias = dim_alias(i)
        aliases[group.key] = alias
        lookup = lookups[group.key]
        if group.grain and group.grain != "day" and report.calendar == "jalali":
            day_alias = f"_gl{i}"
            qs = qs.alias(**{day_alias: local_date(lookup, group.dimension.is_date)})
            lookup = day_alias
        group_exprs[alias] = (
            bucket_expr(group, report.calendar, lookup) if group.grain else F(lookup)
        )
    measure_exprs = {}
    for i, m in enumerate(report.measures):
        alias = measure_alias(i)
        aliases[m.key] = alias
        measure_exprs[alias] = m.expr

    if not group_exprs:
        return CompiledReport(qs, aliases, True, report, measure_exprs, qs, lookups)

    base = qs
    qs = qs.annotate(**group_exprs).values(*group_exprs).annotate(**measure_exprs)

    def order(alias: str, direction: str):
        expr = F(alias)
        return expr.desc(nulls_last=True) if direction == "desc" else expr.asc(nulls_last=True)

    sort = list(report.sort)
    if not sort:  # default: chronological, else biggest first measure first
        sort = [(g.key, "asc") for g in report.groups if g.grain]
        sort = sort or [(report.measures[0].key, "desc")]
    # Remaining group keys make the order total, so LIMIT is deterministic.
    done = {key for key, _ in sort}
    sort += [(g.key, "asc") for g in report.groups if g.key not in done]
    ordering = [order(aliases[key], direction) for key, direction in sort]
    qs = qs.order_by(*ordering)
    return CompiledReport(qs, aliases, False, report, measure_exprs, base, lookups)
