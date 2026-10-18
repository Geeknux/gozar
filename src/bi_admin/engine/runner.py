"""Run a ``ReportSpec``: permission check → validate → compile → execute → serialise."""

from __future__ import annotations

import datetime as dt
import decimal
import time
from dataclasses import dataclass
from typing import Any

from django.db.models import Max, Min
from django.utils.translation import gettext as _

from bi_admin import preferences
from bi_admin.calendar import jalali, table
from bi_admin.engine.compiler import compile_report, local_date
from bi_admin.engine.spec import ReportSpec
from bi_admin.engine.validator import (
    SpecError,
    SpecValidationError,
    ValidatedReport,
    validate,
)
from bi_admin.semantic import semantic_site
from bi_admin.semantic.base import active_language

MAX_SAFE_INT = 2**53


class ReportError(Exception):
    """A report that is valid but cannot run (permissions, missing calendar data)."""

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.payload = {"detail": message, "code": code, **extra}


@dataclass
class ReportResult:
    report: ValidatedReport
    columns: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    truncated: bool
    duration_ms: int

    def as_dict(self) -> dict[str, Any]:
        r = self.report
        return {
            "semantic_model": r.semantic.name,
            "spec": r.spec.model_dump(),
            "chart": r.spec.chart,
            "calendar": r.calendar,
            "time_dimension": r.time_dimension.key if r.time_dimension else None,
            "time_grain": r.time_grain,
            "period": r.period.as_dict() if r.period else None,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": len(self.rows),
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
        }


def number(value: Any) -> Any:
    """JSON-friendly measure value: Decimals become ints when integral, else floats."""
    if isinstance(value, decimal.Decimal):
        if value == value.to_integral_value() and abs(value) < MAX_SAFE_INT:
            return int(value)
        return float(value)
    return value


def cell(value: Any) -> Any:
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return number(value)
    return value


def columns(report: ValidatedReport) -> list[dict[str, Any]]:
    """Column metadata; ``label`` is in the active language, ``labels`` has both."""
    lang = active_language()
    out = []
    for g in report.groups:
        d = g.dimension
        col: dict[str, Any] = {
            "key": d.key,
            "role": "dimension",
            "type": d.type,
            "label": d.label[lang],
            "labels": d.label,
        }
        if g.grain:
            col.update(grain=g.grain, calendar=report.calendar, label_key=f"{d.key}__label")
        if d.choices:
            col["choices"] = [{"value": v, "label": labels[lang]} for v, labels in d.choices]
        out.append(col)
    for m in report.measures:
        out.append(
            {
                "key": m.key,
                "role": "measure",
                "format": m.format,
                "label": m.label[lang],
                "labels": m.label,
            }
        )
    return out


def check_calendar(compiled) -> None:
    """Jalali buckets come from ``CalendarDay``; refuse to return silently-null
    buckets when the table doesn't cover the dates in the report."""
    report = compiled.report
    if report.calendar != "jalali":
        return
    for g in report.groups:
        if not g.grain or g.grain == "day":
            continue
        dim = g.dimension
        expr = local_date(dim.path or dim.expr, dim.is_date)  # aggregate() can't see alias()
        span = compiled.base.aggregate(lo=Min(expr), hi=Max(expr))
        lo, hi = span["lo"], span["hi"]
        if lo is None:
            continue
        if isinstance(lo, dt.datetime):  # some backends return datetimes
            lo, hi = lo.date(), hi.date()
        missing = table.missing_days(lo, hi)
        if missing:
            raise ReportError(
                409,
                "calendar_not_populated",
                _(
                    "The calendar table lacks %(missing)s day(s) between %(start)s and %(end)s; "
                    "run `manage.py gozar_calendar`"
                )
                % {"missing": missing, "start": lo, "end": hi},
                start=lo.isoformat(),
                end=hi.isoformat(),
            )


def authorize(spec: ReportSpec, request) -> None:
    """Unknown and forbidden semantic models are reported without revealing others."""
    if request is None:
        return
    allowed = [s.name for s in semantic_site.all() if s.has_permission(request)]
    if spec.semantic_model not in semantic_site:
        raise SpecValidationError(
            [
                SpecError(
                    "unknown_semantic_model",
                    _("Unknown semantic model %(name)r") % {"name": spec.semantic_model},
                    "semantic_model",
                    spec.semantic_model,
                    allowed,
                )
            ]
        )
    if spec.semantic_model not in allowed:
        raise ReportError(
            403,
            "permission_denied",
            _("Permission denied: report on %(name)s") % {"name": spec.semantic_model},
        )


def run(spec: ReportSpec | dict, request=None, today: dt.date | None = None) -> ReportResult:
    """Validate and execute ``spec``. ``request`` (when given) drives permissions and
    the semantic model's ``get_queryset``; ``today`` pins relative periods."""
    if isinstance(spec, dict):
        spec = ReportSpec.model_validate(spec)
    authorize(spec, request)
    calendar = preferences.get(request).calendar if request is not None else None
    report = validate(spec, today=today, calendar=calendar)
    started = time.monotonic()
    compiled = compile_report(report, request)
    check_calendar(compiled)
    if compiled.aggregate:
        values = compiled.queryset.aggregate(**compiled.measures)
        rows = [{m.key: number(values[compiled.aliases[m.key]]) for m in report.measures}]
        truncated = False
    else:
        fetched = list(compiled.queryset[: report.limit + 1])
        truncated = len(fetched) > report.limit
        rows = [serialise_row(report, compiled.aliases, raw) for raw in fetched[: report.limit]]
    duration = int((time.monotonic() - started) * 1000)
    return ReportResult(report, columns(report), rows, truncated, duration)


def serialise_row(report: ValidatedReport, aliases: dict[str, str], raw: dict) -> dict:
    row: dict[str, Any] = {}
    for g in report.groups:
        value = raw[aliases[g.key]]
        if g.grain:
            if isinstance(value, dt.datetime):
                value = value.date()
            row[g.key] = value.isoformat() if value is not None else None
            row[f"{g.key}__label"] = (
                jalali.bucket_label(value, g.grain, report.calendar) if value is not None else None
            )
        else:
            row[g.key] = cell(value)
    for m in report.measures:
        row[m.key] = number(raw[aliases[m.key]])
    return row
