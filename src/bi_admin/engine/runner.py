"""Run a ``ReportSpec``: permission check → validate → compile → cache → execute.

Execution happens on the reports database (``GOZAR["REPORTS_DATABASE"]``) under
the statement timeout, and every run is written to the audit log.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import time
from dataclasses import dataclass
from typing import Any

from django.db.models import Max, Min
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from bi_admin import preferences
from bi_admin.access import audit
from bi_admin.access.rules import scope_key, scope_text
from bi_admin.calendar import jalali, periods, table
from bi_admin.engine.compiler import CompiledReport, compile_report, local_date
from bi_admin.engine.errors import ReportError
from bi_admin.engine.spec import ReportSpec
from bi_admin.engine.validator import (
    ResolvedFilter,
    SpecError,
    SpecValidationError,
    ValidatedReport,
    max_rows,
    validate,
)
from bi_admin.execution import cache
from bi_admin.execution.limits import statement_timeout
from bi_admin.execution.router import reporting, reports_database
from bi_admin.semantic import semantic_site
from bi_admin.semantic.base import active_language

__all__ = ["ReportError", "ReportResult", "authorize", "run"]

MAX_SAFE_INT = 2**53


@dataclass
class ReportResult:
    report: ValidatedReport
    columns: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    truncated: bool
    duration_ms: int
    cache: dict[str, Any] | None = None  # {"enabled", "hit", "stored_at"}
    scope: str = ""

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
            "compare_to": r.spec.compare_to,
            "comparison": r.comparison.as_dict() if r.comparison else None,
            "columns": self.columns,
            "rows": self.rows,
            "row_count": len(self.rows),
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
            "cache": self.cache or {"enabled": False, "hit": False, "stored_at": None},
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
    if report.comparison is not None:
        for m in report.measures:
            out.extend(comparison_columns(report.comparison.kind, m, lang))
    return out


COMPARISON_SUFFIXES = ("compare", "change", "change_pct")


def _comparison_labels(kind: str, part: str, label: str) -> str:
    if part == "compare":
        if kind == "previous_period":
            return _("%(measure)s (previous period)") % {"measure": label}
        return _("%(measure)s (same period last year)") % {"measure": label}
    if part == "change":
        return _("%(measure)s change") % {"measure": label}
    return _("%(measure)s change %%") % {"measure": label}


def comparison_columns(kind: str, measure, lang: str) -> list[dict[str, Any]]:
    """Per measure: the comparison value, the absolute change and the change as a
    fraction of the comparison value (``format: percent``; null when it is 0)."""
    out = []
    for part in COMPARISON_SUFFIXES:
        labels = {}
        for code in ("fa", "en"):
            with translation.override(code):
                labels[code] = _comparison_labels(kind, part, measure.label[code])
        out.append(
            {
                "key": f"{measure.key}__{part}",
                "role": "comparison",
                "measure": measure.key,
                "comparison": part,
                "format": "percent" if part == "change_pct" else measure.format,
                "label": labels[lang],
                "labels": labels,
            }
        )
    return out


def check_calendar(compiled: CompiledReport, alias: str) -> None:
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
        span = compiled.base.using(alias).aggregate(lo=Min(expr), hi=Max(expr))
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


def prepare(spec: ReportSpec, request=None, today: dt.date | None = None) -> ValidatedReport:
    """Authorise and validate ``spec`` for the user behind ``request``."""
    authorize(spec, request)
    calendar = preferences.get(request).calendar if request is not None else None
    return validate(spec, today=today, calendar=calendar, request=request)


def cache_key(compiled: CompiledReport, alias: str) -> str:
    report = compiled.report
    try:
        sql = str(compiled.queryset.query)
    except Exception:  # EmptyResultSet & co: the report reads only its own model
        sql = ""
    deps = cache.dependencies(sql, alias, [report.semantic.model_label])
    return cache.report_key(
        {
            "spec": report.spec.model_dump(mode="json", exclude={"chart"}),
            "period": report.period.as_dict() if report.period else None,
            "scope": scope_key(compiled.source),
            "language": active_language(),
            "database": alias,
            "data": cache.versions(deps),
        }
    )


def run(
    spec: ReportSpec | dict,
    request=None,
    today: dt.date | None = None,
    *,
    use_cache: bool = True,
    kind: str = "run",
    job=None,
    cache_only: bool = False,
) -> ReportResult | None:
    """Validate and execute ``spec``. ``request`` (when given) drives permissions,
    visibility, row rules and the semantic model's ``get_queryset``; ``None``
    means trusted code (no restrictions). ``today`` pins relative periods;
    ``use_cache=False`` bypasses (but refreshes) the result cache;
    ``cache_only=True`` returns ``None`` instead of executing on a cache miss."""
    if isinstance(spec, dict):
        spec = ReportSpec.model_validate(spec)
    with audit.record(kind, spec, request, job=job) as entry:
        report = prepare(spec, request, today)
        entry.spec = report.spec.model_dump(mode="json")
        compiled = compile_report(report, request)
        scope = scope_text(compiled.source)
        alias = reports_database()
        entry.scope, entry.database = scope, alias

        key = cache_key(compiled, alias) if cache.enabled() else None
        if key is not None and use_cache:
            hit = cache.get(key)
            if hit is not None:
                entry.cache_hit, entry.row_count = True, len(hit["rows"])
                info = {"enabled": True, "hit": True, "stored_at": hit["stored_at"]}
                return ReportResult(
                    report,
                    columns(report),
                    hit["rows"],
                    hit["truncated"],
                    hit["duration_ms"],
                    info,
                    scope,
                )
        if cache_only:
            entry.skip = True
            return None

        started = time.monotonic()
        with reporting(alias), statement_timeout(alias):
            rows, truncated = execute(compiled, alias)
            if report.comparison is not None:
                previous = compile_report(comparison_report(report), request)
                previous_rows, _truncated = execute(previous, alias)
                merge_comparison(report, rows, previous_rows)
        duration = int((time.monotonic() - started) * 1000)
        entry.row_count = len(rows)
        info = {"enabled": key is not None, "hit": False, "stored_at": None}
        if key is not None:
            info["stored_at"] = timezone.now().isoformat()
            cache.put(
                key,
                {
                    "rows": rows,
                    "truncated": truncated,
                    "duration_ms": duration,
                    "stored_at": info["stored_at"],
                },
            )
        return ReportResult(report, columns(report), rows, truncated, duration, info, scope)


def execute(compiled: CompiledReport, alias: str) -> tuple[list[dict], bool]:
    """Rows of a compiled report (inside ``reporting``/``statement_timeout``)."""
    report = compiled.report
    check_calendar(compiled, alias)
    if compiled.aggregate:
        values = compiled.queryset.using(alias).aggregate(**compiled.measures)
        aliases = compiled.aliases
        return [{m.key: number(values[aliases[m.key]]) for m in report.measures}], False
    fetched = list(compiled.queryset.using(alias)[: report.limit + 1])
    rows = [serialise_row(report, compiled.aliases, raw) for raw in fetched[: report.limit]]
    return rows, len(fetched) > report.limit


def comparison_report(report: ValidatedReport) -> ValidatedReport:
    """``report`` over its comparison period: the relative period (or the date range
    filter it came from) replaced by the comparison range; every group, unsorted
    and up to ``REPORT_MAX_ROWS``, so each current row can find its counterpart."""
    comparison = report.comparison
    filters = list(report.filters)
    period = None
    if report.comparison_filter is not None:
        f = filters[report.comparison_filter]
        filters[report.comparison_filter] = ResolvedFilter(
            f.dimension, "between", [comparison.start, comparison.end]
        )
    else:
        period = periods.Period(
            name=report.period.name,
            start=comparison.start,
            end=comparison.end,
            calendar=report.calendar,
        )
    return dataclasses.replace(
        report,
        filters=filters,
        period=period,
        sort=[],
        limit=max_rows(),
        comparison=None,
        comparison_filter=None,
    )


def _change(current: Any, previous: Any) -> tuple[Any, Any]:
    if current is None or previous is None:
        return None, None
    change = current - previous
    if isinstance(change, float):
        change = round(change, 10)
    pct = round(change / abs(previous), 6) if previous else None
    return change, pct


def merge_comparison(report: ValidatedReport, rows: list[dict], previous: list[dict]) -> None:
    """Add ``<measure>__compare``, ``__change`` and ``__change_pct`` to each row.

    Rows match on their group values, with every time bucket of the comparison
    period moved onto the bucket it lines up with (``Comparison.to_current``).
    Groups that only exist in the comparison period are not added. When two
    comparison buckets land on one (a clamped day, e.g. 29 Feb), the exact one wins."""
    comparison = report.comparison
    matches: dict[str, tuple[bool, dict]] = {}
    for row in previous:
        key, exact = [], True
        for g in report.groups:
            value = row[g.key]
            if g.grain and value is not None:
                moved, ok = comparison.to_current(dt.date.fromisoformat(value), g.grain)
                value, exact = moved.isoformat(), exact and ok
            key.append(value)
        k = repr(key)
        if k not in matches or (exact and not matches[k][0]):
            matches[k] = (exact, row)
    for row in rows:
        match = matches.get(repr([row[g.key] for g in report.groups]))
        other = match[1] if match else None
        for m in report.measures:
            value = other[m.key] if other else None
            change, pct = _change(row[m.key], value)
            row[f"{m.key}__compare"] = value
            row[f"{m.key}__change"] = change
            row[f"{m.key}__change_pct"] = pct


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
