"""Dashboards: widget specs and global filters.

A widget is a saved report or an inline spec. Global filters — a period (a
relative period or a date range) and category values such as cities — apply to
every widget whose semantic model has the matching dimension *for the viewer*
(visibility counts); the others show their own data and say so. The effective
spec of each widget then runs through the normal engine (``/reports/run``: the
viewer's permissions and scope, the result cache and background jobs), so
drill-down and export work on it like on any report.
"""

from __future__ import annotations

from typing import Any

from django.utils.translation import gettext as _
from pydantic import ValidationError

from bi_admin import sharing, translations
from bi_admin.calendar import jalali, periods
from bi_admin.engine.spec import ReportSpec
from bi_admin.models import Dashboard, DashboardWidget
from bi_admin.semantic import semantic_site
from bi_admin.semantic.base import active_language

GRID_COLUMNS = 12
MAX_WIDGETS = 30
CHARTS = ("table", "bar", "line", "area", "pie", "pivot", "kpi")
RANGE_OPS = ("between", "gte", "lte", "eq")
FILTER_TYPES = ("category", "geo", "boolean")


class FilterError(ValueError):
    pass


def widget_spec(widget: DashboardWidget, request, owner_id: int) -> tuple[dict | None, str]:
    """The spec a viewer sees for ``widget`` and, when none, why. A saved report is
    used when the viewer may open it, or when the dashboard's owner owns it (they
    shared it by putting it on their shared dashboard)."""
    if widget.saved_report_id is None:
        if widget.spec:
            return dict(widget.spec), ""
        return None, _("The saved report of this widget was deleted.")
    report = widget.saved_report
    if report.owner_id == owner_id or sharing.saved_reports(request).filter(pk=report.pk).exists():
        return dict(report.spec), ""
    return None, _("This saved report is not shared with you.")


def widget_payload(widget: DashboardWidget, request, owner_id: int) -> dict[str, Any]:
    spec, problem = widget_spec(widget, request, owner_id)
    allowed = sharing.reportable_models(request)
    if spec is not None and spec.get("semantic_model") not in allowed:
        spec, problem = None, _("You don't have access to this widget's data.")
    report = widget.saved_report
    return {
        "id": widget.pk,
        # the widget's own title, else its saved report's name (in the viewer's language)
        "title": widget.text("title") or (report.text("name") if report else ""),
        "title_i18n": translations.translations(widget, "title"),
        "saved_report": {"id": report.pk, "name": report.text("name")} if report else None,
        "inline": widget.saved_report_id is None,
        "spec": spec,
        "chart": widget.chart or (spec or {}).get("chart") or "table",
        "problem": problem,
        "layout": {"x": widget.x, "y": widget.y, "w": widget.w, "h": widget.h},
    }


def filter_options(widgets: list[dict], request) -> list[dict[str, Any]]:
    """Category dimensions a global filter may use: those of the widgets' models
    the viewer can see, keyed by dimension key (one entry per key, with the first
    model offering it for value lookups)."""
    lang = active_language()
    seen: dict[str, dict] = {}
    for w in widgets:
        spec = w["spec"]
        if not spec or spec.get("semantic_model") not in semantic_site:
            continue
        sem = semantic_site.get(spec["semantic_model"])
        for key, dim in sem.visible_dimensions(request).items():
            if dim.type in FILTER_TYPES and key not in seen:
                seen[key] = {
                    "key": key,
                    "label": dim.label[lang],
                    "type": dim.type,
                    "semantic_model": sem.name,
                    "choices": [{"value": v, "label": lb[lang]} for v, lb in dim.choices]
                    if dim.choices
                    else None,
                }
    return list(seen.values())


def clean_filters(raw: Any) -> dict[str, Any]:
    """Normalise a global filter object (stored defaults or a viewer's choice)."""
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise FilterError(_("Filters must be an object"))
    out: dict[str, Any] = {}
    period = raw.get("period") or None
    if period is not None:
        if not isinstance(period, str) or not periods.is_valid(period):
            raise FilterError(_("Unknown relative period %(name)r") % {"name": period})
        out["period"] = period
    rng = raw.get("range") or None
    if rng is not None:
        if not isinstance(rng, list) or len(rng) != 2:
            raise FilterError(_("A date range is [start, end]"))
        try:
            start, end = (jalali.parse_date(v) for v in rng)
        except (ValueError, TypeError):
            raise FilterError(_("A date range is [start, end]")) from None
        if start > end:
            raise FilterError(_("The range starts after it ends"))
        if period is not None:
            raise FilterError(_("Choose a period or a date range, not both"))
        out["range"] = [start.isoformat(), end.isoformat()]
    dims = raw.get("dimensions")
    if dims is not None:
        if not isinstance(dims, list) or not all(isinstance(d, str) for d in dims):
            raise FilterError(_("dimensions is a list of dimension keys"))
        out["dimensions"] = list(dict.fromkeys(dims))
    values = raw.get("values") or {}
    if not isinstance(values, dict):
        raise FilterError(_("values maps dimension keys to lists of values"))
    clean_values = {}
    for key, vals in values.items():
        if vals in (None, "", []):
            continue
        vals = vals if isinstance(vals, list) else [vals]
        if not all(isinstance(v, str | int | float | bool) for v in vals) or len(vals) > 100:
            raise FilterError(_("Invalid values for %(key)s") % {"key": key})
        clean_values[key] = vals
    if clean_values:
        out["values"] = clean_values
    return out


def apply_filters(spec: dict, filters: dict, request) -> tuple[dict, list[str], list[str]]:
    """``spec`` with the global ``filters`` applied, plus the filter names applied
    and ignored (``period``, or a dimension key) for this widget.

    The period replaces the widget's relative period and any date filter on its
    time dimension (a comparison is kept, now against the new period); a category
    filter replaces the widget's own filters on that dimension."""
    spec = dict(spec)
    applied: list[str] = []
    ignored: list[str] = []
    name = spec.get("semantic_model")
    if name not in semantic_site:
        return spec, applied, [k for k in ("period", *filters.get("values", {})) if k]
    sem = semantic_site.get(name)
    dims = sem.visible_dimensions(request)
    current = [dict(f) for f in spec.get("filters") or []]

    if filters.get("period") or filters.get("range"):
        time_key = spec.get("time_dimension") or sem.default_time_dimension(request)
        if time_key and time_key in dims:
            current = [
                f for f in current if not (f.get("field") == time_key and f.get("op") in RANGE_OPS)
            ]
            if filters.get("period"):
                spec["relative_period"] = filters["period"]
            else:
                spec["relative_period"] = None
                current.append({"field": time_key, "op": "between", "value": filters["range"]})
            applied.append("period")
        else:
            ignored.append("period")

    for key, values in (filters.get("values") or {}).items():
        dim = dims.get(key)
        if dim is None or dim.type == "time":
            ignored.append(key)
            continue
        current = [f for f in current if f.get("field") != key]
        if len(values) == 1:
            current.append({"field": key, "op": "eq", "value": values[0]})
        else:
            current.append({"field": key, "op": "in", "value": list(values)})
        applied.append(key)
    spec["filters"] = current
    return spec, applied, ignored


def effective_widgets(board: Dashboard, filters: dict, request) -> list[dict[str, Any]]:
    """Every widget of ``board`` for this viewer with the global filters applied."""
    out = []
    for widget in board.widgets.select_related("saved_report"):
        item = widget_payload(widget, request, board.owner_id)
        item["applied"], item["ignored"] = [], []
        if item["spec"] is not None:
            spec, applied, ignored = apply_filters(item["spec"], filters, request)
            spec["chart"] = item["chart"]
            try:
                item["spec"] = ReportSpec.model_validate(spec).model_dump(mode="json")
                item["applied"], item["ignored"] = applied, ignored
            except ValidationError:
                item["spec"], item["problem"] = None, _("This widget's report spec is invalid.")
        out.append(item)
    return out
