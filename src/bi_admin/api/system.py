"""The System page API, for superusers and users with ``bi_admin.view_system``."""

from typing import Literal
from uuid import UUID

from django.utils.translation import gettext as _
from ninja import Router, Status

from bi_admin import system
from bi_admin.crud.service import CrudError
from bi_admin.execution import jobs
from bi_admin.models import ReportJob
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
