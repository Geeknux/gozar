"""Answer a validated report from a pre-aggregation when that is provably exact.

A pre-aggregation can answer a report when

* every measure is one of its measures (all of which are additive);
* every group-by is one of its dimensions, or its time dimension at a grain its
  buckets nest in: day buckets serve every grain in both calendars, coarser
  buckets only their own calendar's same or coarser grains (a Jalali month never
  answers a Gregorian one, and weeks never make months);
* every filter is on one of its dimensions or on its time dimension, and every
  date range — filters, the relative period, the comparison period — starts and
  ends on bucket boundaries (always true for day buckets);
* the user's row-level scope can be applied to it (``bi_admin.scale.scope``); and
* it is built from the current definition and fresh, or stale for less than its
  ``max_staleness``.

The report is then *retargeted*: the same ``ValidatedReport`` over a shadow
semantic model whose dimensions are the summary table's columns and whose
measures are the re-aggregations of the stored parts. The normal compiler turns it
into SQL, so grouping, time buckets in both calendars (the bucket column is a
date: grains are ``Trunc`` of it or a ``CalendarDay`` lookup), filters, sorting,
limits and null handling are the ones the ORM path uses.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from django.db import models

from bi_admin.calendar.periods import unit_start
from bi_admin.engine.validator import GroupKey, ResolvedFilter, ValidatedReport
from bi_admin.models import Materialization
from bi_admin.scale import scope
from bi_admin.scale import state as states
from bi_admin.scale.preaggregations import TIME_COLUMN, PreAggregation


@dataclass
class Refusal:
    engine: str
    code: str
    detail: str = ""
    pre_aggregation: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out = {"engine": self.engine, "code": self.code, "detail": self.detail}
        if self.pre_aggregation:
            out["pre_aggregation"] = self.pre_aggregation
        return out


@dataclass
class Match:
    preagg: PreAggregation
    row: Materialization
    source: models.QuerySet  # the summary rows in the user's scope
    freshness: states.Freshness
    refusals: list[Refusal] = field(default_factory=list)


def aligned(preagg: PreAggregation, start: dt.date | None, end: dt.date | None) -> bool:
    """Whether ``[start, end]`` (local dates; ``None`` = open) covers whole buckets."""
    grain = preagg.grain
    if grain is None:
        return False
    if grain.grain == "day":
        return True
    if start is not None and unit_start(start, grain.grain, grain.calendar) != start:
        return False
    if end is not None:
        after = end + dt.timedelta(days=1)
        if unit_start(after, grain.grain, grain.calendar) != after:
            return False
    return True


def _filter_range(f: ResolvedFilter) -> list[tuple[dt.date | None, dt.date | None]]:
    if f.op in ("eq", "neq"):
        return [(f.value, f.value)]
    if f.op == "between":
        return [(f.value[0], f.value[1])]
    if f.op == "gte":
        return [(f.value, None)]
    if f.op == "lte":
        return [(None, f.value)]
    if f.op in ("in", "not_in"):
        return [(d, d) for d in f.value]
    return []  # is_null


def coverage(preagg: PreAggregation, report: ValidatedReport) -> tuple[str, str] | None:
    """Why ``preagg`` can't answer ``report`` (``(code, detail)``), or ``None``."""
    if report.semantic.name != preagg.semantic.name:
        return "other_model", report.semantic.name
    for m in report.measures:
        if m.key not in preagg.decompositions:
            return "measure_not_included", m.key
    time_key = preagg.time_dim.key if preagg.time_dim is not None else None
    for g in report.groups:
        if g.grain:
            if g.key != time_key:
                return "time_dimension_not_included", g.key
            if not preagg.grain.serves(g.grain, report.calendar):
                return "grain_not_compatible", f"{g.grain} ({report.calendar})"
        elif g.key not in preagg.dims:
            return "dimension_not_included", g.key
    for f in report.filters:
        key = f.dimension.key
        if f.dimension.type == "time":
            if key != time_key:
                return "time_dimension_not_included", key
            for start, end in _filter_range(f):
                if not aligned(preagg, start, end):
                    return "range_not_aligned", key
        elif key not in preagg.dims:
            return "filter_not_included", key
    if report.period is not None and report.time_dimension is not None:
        if report.time_dimension.key != time_key:
            return "time_dimension_not_included", report.time_dimension.key
        if not aligned(preagg, report.period.start, report.period.end):
            return "range_not_aligned", report.period.name
    return None


def columns_by_path(preagg: PreAggregation) -> dict[str, str]:
    return {c.path: c.name for c in preagg.columns if c.path}


def scoped_source(preagg: PreAggregation, row: Materialization, request) -> models.QuerySet:
    """The summary rows the user may see; ``scope.Untranslatable`` when the scope
    can't be applied exactly."""
    sem = preagg.semantic
    if not scope.same_base(sem, request):
        raise scope.Untranslatable("custom_queryset", "get_queryset differs for this user")
    model = preagg.model_for(row.table)
    qs = model._default_manager.all()
    q = scope.rule_q(sem, request) if request is not None else None
    if q is None:
        return qs
    return qs.filter(scope.to_columns(sem.model, q, columns_by_path(preagg)))


def find(reports: list[ValidatedReport], request) -> tuple[Match | None, list[Refusal]]:
    """The smallest usable pre-aggregation answering every report in ``reports`` (the
    report and its comparison), plus why the others were refused."""
    report = reports[0]
    preaggs = list(getattr(report.semantic, "pre_aggregations", []))
    if not preaggs:
        return None, []
    rows = states.states(Materialization.Kind.PREAGGREGATION)
    refusals: list[Refusal] = []
    matches: list[Match] = []
    for preagg in preaggs:

        def refuse(code: str, detail: str = "", preagg=preagg) -> None:
            refusals.append(Refusal("preaggregation", code, detail, preagg.name))

        why = next((w for w in (coverage(preagg, r) for r in reports) if w), None)
        if why is not None:
            refuse(*why)
            continue
        row = rows.get(preagg.key)
        if row is None or row.status != Materialization.Status.READY or not row.table:
            refuse("not_built")
            continue
        if row.definition != preagg.definition:
            refuse("definition_changed")
            continue
        try:
            source = scoped_source(preagg, row, request)
        except scope.Untranslatable as exc:
            refuse(exc.code, exc.detail)
            continue
        ok, found = states.usable(row, preagg.setting("MAX_STALENESS"))
        if not ok:
            refuse("stale", f"{found.lag_seconds or 0:.0f}s" if found.lag_seconds else "")
            continue
        matches.append(Match(preagg, row, source, found))
    if not matches:
        return None, refusals
    best = min(matches, key=lambda m: (m.row.row_count or 0, m.preagg.name))
    return best, refusals


# --- retargeting ---------------------------------------------------------------------


class ShadowSemantic:
    """What the compiler needs of a semantic model, over a summary table."""

    def __init__(self, original, model, dimensions: dict[str, Any]):
        self.name = original.name
        self.model = model
        self.model_label = original.model_label
        self.dimensions = dimensions
        self.pre_aggregations: list = []


def retarget(report: ValidatedReport, preagg: PreAggregation, model) -> ValidatedReport:
    """``report`` over the summary table of ``preagg`` (see the module docstring)."""
    by_key = {c.key: c.name for c in preagg.columns if c.role == "dimension"}
    time_key = preagg.time_dim.key if preagg.time_dim is not None else None
    shadows: dict[str, Any] = {}

    def shadow(dim):
        if dim.key not in shadows:
            clone = copy.copy(dim)
            if dim.key == time_key:
                clone.expr, clone.is_date = TIME_COLUMN, True
            else:
                clone.expr = by_key[dim.key]
            clone.steps = []
            shadows[dim.key] = clone
        return shadows[dim.key]

    groups = [GroupKey(shadow(g.dimension), g.grain) for g in report.groups]
    filters = [ResolvedFilter(shadow(f.dimension), f.op, f.value) for f in report.filters]
    time_dim = report.time_dimension
    if time_dim is not None and report.period is not None:
        time_dim = shadow(time_dim)
    measures = []
    for m in report.measures:
        clone = copy.copy(m)
        clone.expr = preagg.decompositions[m.key].expression(lambda part: part.column)
        measures.append(clone)
    return dataclasses.replace(
        report,
        semantic=ShadowSemantic(report.semantic, model, shadows),
        measures=measures,
        groups=groups,
        filters=filters,
        time_dimension=time_dim,
    )
