"""Drill-down: the records behind one cell of a report.

A clicked cell is the report's ``ReportSpec`` plus the values of (some of) its
group keys, e.g. ``{"city": "Tehran", "created_at": "2026-09-01"}`` for the
September bar of Tehran. Each value becomes an extra filter on the spec — a
time bucket becomes a date range covering the bucket — and the spec goes
through the normal validator, so lookups, value coercion, permissions, row
rules, visibility and the semantic model's ``get_queryset`` apply exactly as for
the report itself. List columns that only invisible dimensions/measures expose
are left out of the rows.
Clicking a measure also applies that measure's own aggregate filter (e.g.
``status="paid"`` for revenue) so the record count matches the cell.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from django.core.paginator import EmptyPage, Paginator
from django.db.models import Q
from django.utils.encoding import force_str
from django.utils.translation import gettext as _

from bi_admin.access import audit
from bi_admin.access.rules import scope_text
from bi_admin.calendar import jalali, periods
from bi_admin.discovery import get_registry
from bi_admin.engine.compiler import compile_report
from bi_admin.engine.runner import ReportError, prepare
from bi_admin.engine.spec import Filter, ReportSpec
from bi_admin.engine.validator import SpecError, SpecValidationError, validate
from bi_admin.execution.limits import statement_timeout
from bi_admin.execution.router import reporting, reports_database

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200
_FILTER_PATH = re.compile(r"^filters\[(\d+)\]")


def bucket_range(start: dt.date, grain: str, calendar: str) -> tuple[dt.date, dt.date]:
    """Inclusive first/last day of the time bucket that begins on ``start``."""
    if grain == "day":
        return start, start
    first = periods.unit_start(start, grain, calendar)
    if first != start:
        raise ValueError(f"{start} does not start a {grain}")
    return start, periods.shift(start, grain, 1, calendar) - dt.timedelta(days=1)


def _cell_filters(report, cell: dict[str, Any]) -> tuple[list[Filter], list[SpecError]]:
    groups = {g.key: g for g in report.groups}
    filters, errors = [], []
    for key, value in cell.items():
        path = f"cell.{key}"
        group = groups.get(key)
        if group is None:
            errors.append(
                SpecError(
                    "not_a_group_key",
                    _("%(key)r is not a dimension of this report") % {"key": key},
                    path,
                    key,
                    sorted(groups),
                )
            )
            continue
        if value is None:
            filters.append(Filter(field=key, op="is_null", value=True))
        elif group.grain:
            try:
                start, end = bucket_range(jalali.parse_date(value), group.grain, report.calendar)
            except ValueError:
                errors.append(
                    SpecError(
                        "invalid_value",
                        _("%(value)r is not the start of a %(grain)s")
                        % {"value": value, "grain": group.grain},
                        path,
                        value,
                    )
                )
                continue
            filters.append(
                Filter(field=key, op="between", value=[start.isoformat(), end.isoformat()])
            )
        else:
            filters.append(Filter(field=key, op="eq", value=value))
    return filters, errors


def comparison_target(report, cell: dict[str, Any]) -> tuple[ReportSpec, dict[str, Any]]:
    """The spec and cell behind a comparison value: the report over its comparison
    period, with the cell's time buckets moved to the buckets they were compared with."""
    comparison = report.comparison
    if comparison is None:
        raise SpecValidationError(
            [SpecError("no_comparison", _("This report has no comparison period"), "comparison")]
        )
    spec = report.spec
    span = Filter(
        field=report.time_dimension.key,
        op="between",
        value=[comparison.start.isoformat(), comparison.end.isoformat()],
    )
    filters = list(spec.filters)
    if report.comparison_filter is not None:
        filters[report.comparison_filter] = span
    else:
        filters.append(span)
    target = spec.model_copy(
        update={"filters": filters, "relative_period": None, "compare_to": None}
    )
    grains = {g.key: g.grain for g in report.groups if g.grain}
    moved = dict(cell)
    for key, value in cell.items():
        if grains.get(key) and value is not None:
            try:
                bucket = comparison.to_comparison(jalali.parse_date(value), grains[key])
            except ValueError:
                continue  # left as is: the normal cell validation reports it
            moved[key] = bucket.isoformat() if bucket else value
    return target, moved


def drilldown_queryset(
    spec: ReportSpec,
    cell: dict[str, Any],
    measure: str | None,
    request=None,
    today: dt.date | None = None,
    comparison: bool = False,
):
    """The filtered queryset of the semantic model behind ``cell`` (with
    ``comparison``: behind the cell's comparison value).

    Returns ``(queryset, validated_report, scoped_rows)``; raises
    ``SpecValidationError`` with errors addressed as ``cell.<key>`` / ``measure``
    when the cell doesn't fit."""
    report = prepare(spec, request, today)
    if comparison:
        spec, cell = comparison_target(report, cell)
        report = prepare(spec, request, today)
    extra, errors = _cell_filters(report, cell)
    if measure is not None and measure not in {m.key for m in report.measures}:
        errors.append(
            SpecError(
                "unknown_measure",
                _("%(key)r is not a measure of this report") % {"key": measure},
                "measure",
                measure,
                [m.key for m in report.measures],
            )
        )
    if errors:
        raise SpecValidationError(errors)

    base = len(spec.filters)
    narrowed = report.spec.model_copy(update={"filters": [*report.spec.filters, *extra]})
    try:
        narrowed_report = validate(narrowed, today=today, calendar=report.calendar, request=request)
    except SpecValidationError as exc:
        keys = list(cell)
        for err in exc.errors:  # point errors from the added filters at the cell keys
            m = _FILTER_PATH.match(err.path)
            if m and int(m.group(1)) >= base:
                err.path = f"cell.{keys[int(m.group(1)) - base]}"
        raise
    compiled = compile_report(narrowed_report, request)
    qs = compiled.base
    if measure is not None:
        condition = getattr(report.semantic.measures[measure].expr, "filter", None)
        if isinstance(condition, Q):
            qs = qs.filter(condition)
    if any(f.dimension.fans_out for f in narrowed_report.filters):
        qs = qs.distinct()
    return qs, narrowed_report, compiled.source


def drilldown(
    spec: ReportSpec,
    cell: dict[str, Any],
    measure: str | None = None,
    request=None,
    page: int = 1,
    page_size: int | None = None,
    today: dt.date | None = None,
    comparison: bool = False,
) -> dict[str, Any]:
    """One page of the records behind ``cell``, serialised like the CRUD list."""
    with audit.record("drilldown", spec, request) as entry:
        return _drilldown(entry, spec, cell, measure, request, page, page_size, today, comparison)


def _drilldown(
    entry, spec, cell, measure, request, page, page_size, today, comparison
) -> dict[str, Any]:
    from bi_admin.crud import service

    qs, report, source = drilldown_queryset(spec, cell, measure, request, today, comparison)
    entry.spec = report.spec.model_dump(mode="json")
    entry.scope = scope_text(source)
    entry.database = alias = reports_database()
    label = report.semantic.model_label
    options = service.site.options_for(label) if label in get_registry() else None
    if options is not None:
        hidden = report.semantic.hidden_fields(request)
        cols = [c for c in service.list_columns(options) if not _exposes(c.path, hidden)]
        qs = service.apply_ordering(options, qs, None)
        related = service.select_related_paths(options, cols)
        if related:
            qs = qs.select_related(*related)
    else:
        cols = []
        qs = qs.order_by("pk")
    size = max(1, min(page_size or DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE))
    model = report.semantic.model
    with reporting(alias), statement_timeout(alias):
        paginator = Paginator(qs.using(alias), size)
        try:
            current = paginator.page(page)
        except EmptyPage:
            raise ReportError(
                404, "page_not_found", _("Page %(page)s does not exist") % {"page": page}
            ) from None
        if options is not None:
            rows = service.serialize_rows(options, request, cols, current.object_list)
        else:
            rows = [
                {"pk": service.json_value(o.pk), "label": force_str(o), "cells": []}
                for o in current.object_list
            ]
    entry.row_count = paginator.count
    return {
        "model": label,
        "verbose_name": force_str(model._meta.verbose_name),
        "verbose_name_plural": force_str(model._meta.verbose_name_plural),
        "linkable": options is not None,
        "cell": cell,
        "measure": measure,
        "comparison": comparison,
        "filters": [f.model_dump() for f in report.spec.filters],
        "columns": [c.as_dict() for c in cols],
        "rows": rows,
        "count": paginator.count,
        "page": current.number,
        "page_size": size,
        "num_pages": paginator.num_pages,
    }


def _exposes(path: str | None, hidden: set[str]) -> bool:
    """A column shows a hidden path itself, or a relation whose label may be it."""
    if not path:
        return False
    return any(h == path or h.startswith(path + "__") for h in hidden)
