"""The System page API, for superusers and users with ``bi_admin.view_system``."""

from typing import Literal
from uuid import UUID

from django.core.paginator import EmptyPage, Paginator
from django.db.models import Q
from django.utils.translation import gettext as _
from ninja import Router, Schema, Status

from bi_admin import alerts, system
from bi_admin.alerts import sources as alert_sources
from bi_admin.crud.service import CrudError
from bi_admin.execution import jobs
from bi_admin.models import Alert, AlertSource, ReportJob
from bi_admin.scale import columnar
from bi_admin.scale.preaggregations import all_preaggregations
from bi_admin.system import db
from bi_admin.system import scale as scale_status

router = Router(tags=["system"])


def _require(request) -> None:
    if not system.can_view(request.user):
        raise CrudError(
            403, _("You don't have access to the system dashboard."), code="permission_denied"
        )


@router.get("/overview")
def overview(request, refresh: bool = False):
    """Server, versions, apps, cache, job queue, users, activity, report usage,
    deliveries, media storage and migrations."""
    _require(request)
    return system.overview(refresh)


@router.get("/database")
def database(request, refresh: bool = False):
    """Every table with its model, estimated rows, sizes and indexes."""
    _require(request)
    return system.database(refresh)


@router.get("/database/count")
def count(request, table: str):
    """Exact ``COUNT(*)`` of one table (it must be in the introspected table list)."""
    _require(request)
    try:
        return db.exact_count(table)
    except db.UnknownTable:
        raise CrudError(
            404, _("Unknown table %(table)r") % {"table": table}, code="unknown_table"
        ) from None


@router.get("/checks")
def checks(request, refresh: bool = False):
    """Django's system checks, the deployment checks included."""
    _require(request)
    return system.system_checks(refresh)


# --- Scale (M7) ----------------------------------------------------------------------


@router.get("/scale")
def scale(request):
    """Pre-aggregations (size, rows, last refresh, staleness, hits), columnar copies
    (file, last sync) and the latest refresh jobs."""
    _require(request)
    return scale_status.overview()


@router.post("/scale/preaggregations/{key}/refresh", response={202: dict})
def refresh_preaggregation(
    request, key: str, mode: Literal["auto", "incremental", "rebuild"] = "auto"
):
    """Queue a refresh (``auto``: incremental when declared) or a full rebuild."""
    _require(request)
    found = next((p for p in all_preaggregations() if p.key == key), None)
    if found is None:
        raise CrudError(
            404, _("Unknown pre-aggregation %(name)r") % {"name": key}, code="not_found"
        )
    job = jobs.submit_maintenance(
        request,
        ReportJob.Kind.PREAGGREGATE,
        {"target": key, "mode": mode, "semantic_model": found.semantic.name},
    )
    return Status(202, {"job": jobs.payload(job)})


@router.post("/scale/columnar/{name}/sync", response={202: dict})
def sync_columnar(request, name: str):
    """Queue a sync of a columnar engine's copy."""
    _require(request)
    if name not in columnar.names():
        raise CrudError(404, _("Unknown engine %(name)r") % {"name": name}, code="not_found")
    job = jobs.submit_maintenance(request, ReportJob.Kind.COLUMNAR_SYNC, {"target": name})
    return Status(202, {"job": jobs.payload(job)})


@router.get("/scale/jobs/{job_id}")
def scale_job(request, job_id: UUID):
    """A refresh or sync job (any System page user may follow it)."""
    _require(request)
    job = ReportJob.objects.exclude(kind=ReportJob.Kind.REPORT).filter(pk=job_id).first()
    if job is None:
        raise CrudError(404, _("Job not found"), code="job_not_found")
    return jobs.payload(job)


# --- alerts --------------------------------------------------------------------------


ALERT_PAGE_SIZE = 200


class SourceIn(Schema):
    enabled: bool | None = None  # null: back to the settings' default
    min_severity: str | None = None


def alert_entry(alert: Alert) -> dict:
    details = dict(alert.details or {})
    return {
        "id": alert.pk,
        "created_at": alert.created_at.isoformat(),
        "last_seen_at": alert.last_seen_at.isoformat() if alert.last_seen_at else None,
        "source": alert.source,
        "key": alert.key,
        "severity": alert.severity,
        "severity_display": alert.get_severity_display(),
        "kind": alert.kind,
        "title": alert.title,
        "message": alert.message,
        "details": details,
        "release": alert.release,
        "repeats": alert.repeats,
        "suppressed_before": alert.suppressed_before,
        "delivery": alert.delivery,
        "delivery_display": alert.get_delivery_display(),
        "delivered_at": alert.delivered_at.isoformat() if alert.delivered_at else None,
        "acknowledged_at": alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
        "acknowledged_by": (
            alert.acknowledged_by.get_username() if alert.acknowledged_by_id else None
        ),
        "acknowledged_via": (alert.acknowledged_via or "").split(":", 1)[0] or None,
        "deliveries": [
            {
                "channel": d.channel,
                "status": d.status,
                "attempts": d.attempts,
                "next_attempt_at": d.next_attempt_at.isoformat(),
                "sent_at": d.sent_at.isoformat() if d.sent_at else None,
                "error": d.last_error[:300],
            }
            for d in alert.deliveries.all()
        ],
    }


@router.get("/alerts/status")
def alerts_status(request):
    """The alert channels (configured or not — never the token or the chat ids),
    sources with their switches and minimum severities, the outbox and the worker."""
    _require(request)
    return alerts.status()


@router.get("/alerts")
def alert_list(
    request,
    page: int = 1,
    page_size: int = 50,
    severity: str = "",
    source: str = "",
    kind: str = "",
    delivery: str = "",
    acknowledged: str = "",
    q: str = "",
    id: int | None = None,
):
    """Recorded alerts, newest first; every parameter narrows the list."""
    _require(request)
    qs = Alert.objects.select_related("acknowledged_by").prefetch_related("deliveries")
    for name, value in {
        "severity": severity,
        "source": source,
        "kind": kind,
        "delivery": delivery,
    }.items():
        if value:
            qs = qs.filter(**{name: value})
    if acknowledged in ("true", "false"):
        qs = qs.filter(acknowledged_at__isnull=acknowledged == "false")
    if id is not None:
        qs = qs.filter(pk=id)
    for term in q.split():
        qs = qs.filter(
            Q(title__icontains=term) | Q(message__icontains=term) | Q(key__icontains=term)
        )
    paginator = Paginator(qs, max(1, min(page_size, ALERT_PAGE_SIZE)))
    try:
        current = paginator.page(page)
    except EmptyPage:
        raise CrudError(404, _("Page %(page)s does not exist") % {"page": page}) from None
    return {
        "entries": [alert_entry(a) for a in current.object_list],
        "count": paginator.count,
        "page": current.number,
        "num_pages": paginator.num_pages,
        "filters": {
            "severity": [{"value": v, "label": str(lb)} for v, lb in Alert.Severity.choices],
            "kind": [{"value": v, "label": str(lb)} for v, lb in Alert.Kind.choices],
            "delivery": [{"value": v, "label": str(lb)} for v, lb in Alert.Delivery.choices],
            "source": [
                {"value": n, "label": str(s.label)} for n, s in alert_sources.SOURCES.items()
            ],
        },
    }


@router.post("/alerts/test", response={202: dict})
def alert_test(request):
    """Queue a test alert; ``gozar_worker`` sends it."""
    _require(request)
    alert = alerts.send_test(request.user)
    if alert is None:
        raise CrudError(
            409, _("Test alerts are switched off (or alerts are disabled)."), code="disabled"
        )
    return Status(202, {"alert": alert_entry(alert)})


@router.post("/alerts/{alert_id}/ack")
def alert_ack(request, alert_id: int):
    """Acknowledge an alert (once; later calls leave the first acknowledgement)."""
    _require(request)
    alert = Alert.objects.filter(pk=alert_id).first()
    if alert is None:
        raise CrudError(404, _("Alert not found"), code="not_found")
    alerts.acknowledge(alert, user=request.user, via="web")
    alert.refresh_from_db()
    return alert_entry(alert)


@router.patch("/alerts/sources/{name}")
def alert_source(request, name: str, data: SourceIn):
    """Switch a source on or off and set its minimum severity (``null``: back to
    the settings' value)."""
    _require(request)
    if name not in alert_sources.SOURCES:
        raise CrudError(404, _("Unknown alert source %(name)r") % {"name": name}, code="not_found")
    fields = data.model_dump(exclude_unset=True)
    minimum = fields.get("min_severity")
    if minimum not in (None, "") and minimum not in alert_sources.SEVERITIES:
        raise CrudError(400, _("Unknown severity %(name)r") % {"name": minimum}, code="invalid")
    row, _created = AlertSource.objects.get_or_create(source=name)
    if "enabled" in fields:
        row.enabled = fields["enabled"]
    if "min_severity" in fields:
        row.min_severity = minimum or ""
    row.updated_by = request.user
    row.save()
    return alerts.status()
