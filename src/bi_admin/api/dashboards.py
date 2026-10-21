"""Dashboards: a grid of widgets (saved reports or inline specs) with global filters.

``POST /dashboards/{id}/resolve`` returns each widget's effective spec for the
viewer with the chosen global filters applied; the SPA runs them in parallel
through ``/reports/run`` (cache and background jobs included), and drill-down and
export work on them like on any report."""

from typing import Any

from django.db import transaction
from django.db.models import Count
from django.utils.translation import gettext as _
from ninja import Router, Schema, Status

from bi_admin import dashboards, preferences, sharing, translations
from bi_admin.api.saved import SharingIn, Texts, copy_texts, set_texts, stored_spec
from bi_admin.engine import ReportError, ReportSpec, SpecValidationError
from bi_admin.models import Dashboard, DashboardWidget

router = Router(tags=["dashboards"])


class WidgetIn(Schema):
    title: str = ""
    title_i18n: Texts = None
    saved_report: int | None = None
    spec: ReportSpec | None = None
    chart: str = ""
    x: int = 0
    y: int = 0
    w: int = 6
    h: int = 4


class DashboardIn(Schema):
    name: str = ""
    name_i18n: Texts = None
    description: str = ""
    description_i18n: Texts = None
    sharing: SharingIn | None = None
    filters: dict[str, Any] = {}
    widgets: list[WidgetIn] = []


class DashboardPatch(Schema):
    name: str | None = None
    name_i18n: Texts = None
    description: str | None = None
    description_i18n: Texts = None
    sharing: SharingIn | None = None
    filters: dict[str, Any] | None = None
    widgets: list[WidgetIn] | None = None


class ResolveIn(Schema):
    filters: dict[str, Any] = {}


def home_dashboard(request) -> int | None:
    value = preferences.get(request).options.get("home_dashboard")
    return value if isinstance(value, int) else None


def summary(board: Dashboard, request) -> dict:
    out = sharing.common_payload(board, request)
    out["widget_count"] = getattr(board, "widget_count", None)
    out["is_home"] = home_dashboard(request) == board.pk
    return out


def detail(board: Dashboard, request) -> dict:
    out = summary(board, request)
    widgets = [
        dashboards.widget_payload(w, request, board.owner_id)
        for w in board.widgets.select_related("saved_report")
    ]
    out["widget_count"] = len(widgets)
    out["widgets"] = widgets
    out["filters"] = dashboards.clean_filters(board.filters)
    out["filter_options"] = dashboards.filter_options(widgets, request)
    return out


def _filters(raw) -> dict:
    try:
        return dashboards.clean_filters(raw)
    except dashboards.FilterError as exc:
        raise sharing.SharingError({"filters": [str(exc)]}) from None


def _widgets(items: list[WidgetIn], request) -> list[DashboardWidget]:
    """Validate the widgets for the editing user (unsaved instances)."""
    errors: dict[str, list[str]] = {}
    if len(items) > dashboards.MAX_WIDGETS:
        errors["widgets"] = [_("At most %(n)s widgets") % {"n": dashboards.MAX_WIDGETS}]
    readable = sharing.saved_reports(request)
    out = []
    for i, item in enumerate(items):
        path = f"widgets[{i}]"
        widget = DashboardWidget(chart=item.chart)
        try:
            set_texts(widget, item, fields=("title",), required=None)
        except sharing.SharingError as exc:
            errors.update({f"{path}.{k}": v for k, v in exc.payload["errors"].items()})
        if (item.saved_report is None) == (item.spec is None):
            errors[path] = [_("Choose a saved report or give a report spec")]
        elif item.saved_report is not None:
            widget.saved_report = readable.filter(pk=item.saved_report).first()
            if widget.saved_report is None:
                errors[f"{path}.saved_report"] = [_("Saved report not found")]
        else:
            try:
                widget.spec = stored_spec(item.spec, request)
            except SpecValidationError as exc:
                errors[f"{path}.spec"] = [f"{e.path}: {e.message}" for e in exc.errors]
            except ReportError as exc:
                errors[f"{path}.spec"] = [str(exc)]
        if item.chart and item.chart not in dashboards.CHARTS:
            errors[f"{path}.chart"] = [_("Unknown chart type")]
        cols = dashboards.GRID_COLUMNS
        if not (1 <= item.w <= cols and 0 <= item.x and item.x + item.w <= cols):
            errors[f"{path}.layout"] = [_("A widget must fit the %(n)s-column grid") % {"n": cols}]
        if not (1 <= item.h <= 24 and 0 <= item.y <= 1000):
            errors[f"{path}.layout"] = [_("Invalid widget height or row")]
        widget.x, widget.y, widget.w, widget.h = item.x, item.y, item.w, item.h
        out.append(widget)
    if errors:
        raise sharing.SharingError(errors)
    return out


def _replace_widgets(board: Dashboard, widgets: list[DashboardWidget]) -> None:
    board.widgets.all().delete()
    for w in widgets:
        w.dashboard = board
        translations.sync(w, "title")  # bulk_create skips save()
    DashboardWidget.objects.bulk_create(widgets)


@router.get("")
def list_dashboards(request, q: str = ""):
    qs = sharing.dashboards(request).annotate(widget_count=Count("widgets", distinct=True))
    if q:
        qs = qs.filter(translations.search_q(q, "name", "description"))
    qs = qs.prefetch_related("shared_users", "shared_groups")
    return {"dashboards": [summary(b, request) for b in qs], "home": home_dashboard(request)}


@router.post("", response={201: dict})
def create_dashboard(request, data: DashboardIn):
    widgets = _widgets(data.widgets, request)
    board = Dashboard(owner=request.user, filters=_filters(data.filters))
    set_texts(board, data)
    with transaction.atomic():
        board.save()
        _replace_widgets(board, widgets)
        sharing.apply_sharing(board, data.sharing.dict() if data.sharing else None)
    return Status(201, detail(board, request))


@router.get("/{dashboard_id}")
def get_dashboard(request, dashboard_id: int):
    """The dashboard for this viewer: widgets they can't use say why."""
    return detail(sharing.get_dashboard(request, dashboard_id), request)


@router.patch("/{dashboard_id}")
def update_dashboard(request, dashboard_id: int, data: DashboardPatch):
    """Owner only; ``widgets`` (when given) replaces every widget."""
    board = sharing.editable(sharing.get_dashboard(request, dashboard_id), request)
    widgets = _widgets(data.widgets, request) if data.widgets is not None else None
    set_texts(board, data)
    with transaction.atomic():
        if data.filters is not None:
            board.filters = _filters(data.filters)
        board.save()
        if widgets is not None:
            _replace_widgets(board, widgets)
        sharing.apply_sharing(board, data.sharing.dict() if data.sharing else None)
    return detail(board, request)


@router.delete("/{dashboard_id}")
def delete_dashboard(request, dashboard_id: int):
    board = sharing.editable(sharing.get_dashboard(request, dashboard_id), request)
    board.delete()
    return {"deleted": True}


@router.post("/{dashboard_id}/duplicate", response={201: dict})
def duplicate_dashboard(request, dashboard_id: int, name: str = ""):
    """A private copy owned by the current user, with the widgets this user can
    see (a saved report that isn't shared with them is copied as an inline spec)."""
    source = sharing.get_dashboard(request, dashboard_id)
    readable = set(sharing.saved_reports(request).values_list("pk", flat=True))
    copy = Dashboard(owner=request.user, filters=source.filters)
    copy_texts(source, copy, name)
    with transaction.atomic():
        copy.save()
        widgets = []
        for w in source.widgets.select_related("saved_report"):
            spec, _problem = dashboards.widget_spec(w, request, source.owner_id)
            if spec is None:
                continue
            keep = w.saved_report_id in readable
            titles = translations.translations(w, "title")
            if not titles and w.saved_report:  # the copy may lose access to the report
                titles = translations.translations(w.saved_report, "name")
            widget = DashboardWidget(
                dashboard=copy,
                title_i18n=titles,
                saved_report=w.saved_report if keep else None,
                spec=None if keep else spec,
                chart=w.chart,
                x=w.x,
                y=w.y,
                w=w.w,
                h=w.h,
            )
            translations.sync(widget, "title")
            widgets.append(widget)
        DashboardWidget.objects.bulk_create(widgets)
    return Status(201, detail(copy, request))


@router.post("/{dashboard_id}/resolve")
def resolve(request, dashboard_id: int, data: ResolveIn):
    """Each widget's effective spec with the global ``filters`` applied (when a
    widget's model has the dimension), plus which filters applied to it."""
    board = sharing.get_dashboard(request, dashboard_id)
    filters = _filters(data.filters)
    filters.pop("dimensions", None)
    return {"filters": filters, "widgets": dashboards.effective_widgets(board, filters, request)}
