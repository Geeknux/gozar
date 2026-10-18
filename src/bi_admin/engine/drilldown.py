"""Drill-down: the records behind one cell of a report.

A clicked cell is the report's ``ReportSpec`` plus the values of (some of) its
group keys, e.g. ``{"city": "Tehran", "created_at": "2026-09-01"}`` for the
September bar of Tehran. Each value becomes an extra filter on the spec — a
time bucket becomes a date range covering the bucket — and the spec goes
through the normal validator, so lookups, value coercion, permissions and the
semantic model's ``get_queryset`` apply exactly as for the report itself.
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

from bi_admin import preferences
from bi_admin.calendar import jalali, periods
from bi_admin.discovery import get_registry
from bi_admin.engine.compiler import compile_report
from bi_admin.engine.runner import ReportError, authorize
from bi_admin.engine.spec import Filter, ReportSpec
from bi_admin.engine.validator import SpecError, SpecValidationError, validate

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


def drilldown_queryset(
    spec: ReportSpec,
    cell: dict[str, Any],
    measure: str | None,
    request=None,
    today: dt.date | None = None,
):
    """The filtered queryset of the semantic model behind ``cell``.

    Returns ``(queryset, validated_report)``; raises ``SpecValidationError`` with
    errors addressed as ``cell.<key>`` / ``measure`` when the cell doesn't fit."""
    authorize(spec, request)
    calendar = preferences.get(request).calendar if request is not None else None
    report = validate(spec, today=today, calendar=calendar)
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
        narrowed_report = validate(narrowed, today=today, calendar=report.calendar)
    except SpecValidationError as exc:
        keys = list(cell)
        for err in exc.errors:  # point errors from the added filters at the cell keys
            m = _FILTER_PATH.match(err.path)
            if m and int(m.group(1)) >= base:
                err.path = f"cell.{keys[int(m.group(1)) - base]}"
        raise
    qs = compile_report(narrowed_report, request).base
    if measure is not None:
        condition = getattr(report.semantic.measures[measure].expr, "filter", None)
        if isinstance(condition, Q):
            qs = qs.filter(condition)
    if any(f.dimension.fans_out for f in narrowed_report.filters):
        qs = qs.distinct()
    return qs, narrowed_report


def drilldown(
    spec: ReportSpec,
    cell: dict[str, Any],
    measure: str | None = None,
    request=None,
    page: int = 1,
    page_size: int | None = None,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """One page of the records behind ``cell``, serialised like the CRUD list."""
    from bi_admin.crud import service

    qs, report = drilldown_queryset(spec, cell, measure, request, today)
    label = report.semantic.model_label
    options = service.site.options_for(label) if label in get_registry() else None
    if options is not None:
        cols = service.list_columns(options)
        qs = service.apply_ordering(options, qs, None)
        related = service.select_related_paths(options, cols)
        if related:
            qs = qs.select_related(*related)
    else:
        cols = []
        qs = qs.order_by("pk")
    size = max(1, min(page_size or DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE))
    paginator = Paginator(qs, size)
    try:
        current = paginator.page(page)
    except EmptyPage:
        raise ReportError(
            404, "page_not_found", _("Page %(page)s does not exist") % {"page": page}
        ) from None
    model = report.semantic.model
    return {
        "model": label,
        "verbose_name": force_str(model._meta.verbose_name),
        "verbose_name_plural": force_str(model._meta.verbose_name_plural),
        "linkable": options is not None,
        "cell": cell,
        "measure": measure,
        "filters": [f.model_dump() for f in report.spec.filters],
        "columns": [c.as_dict() for c in cols],
        "rows": [
            {
                "pk": service.json_value(obj.pk),
                "label": force_str(obj),
                "cells": [service.cell_value(options, c, obj) for c in cols] if options else [],
            }
            for obj in current.object_list
        ],
        "count": paginator.count,
        "page": current.number,
        "page_size": size,
        "num_pages": paginator.num_pages,
    }
