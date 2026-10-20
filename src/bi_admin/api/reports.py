"""Report endpoints: catalog, validation, execution (sync or as a background job),
value lookups, drill-down and export."""

from typing import Literal
from uuid import UUID

from django.http import HttpResponse
from django.utils.translation import gettext as _
from ninja import Router, Schema, Status

from bi_admin.access import audit
from bi_admin.engine import ReportError, ReportSpec, SpecValidationError, run
from bi_admin.engine.drilldown import drilldown
from bi_admin.engine.export import export
from bi_admin.engine.runner import prepare
from bi_admin.engine.spec import Scalar
from bi_admin.engine.values import dimension_values
from bi_admin.execution import jobs
from bi_admin.models import ReportJob
from bi_admin.nl.interpret import describe
from bi_admin.semantic import catalog, semantic_site
from bi_admin.semantic.base import active_language

router = Router(tags=["reports"])

RunMode = Literal["auto", "sync", "async"]
ExportFormat = Literal["csv", "xlsx"]


@router.get("/catalog")
def report_catalog(request, lang: str | None = None):
    """Semantic models the user may report on, with the dimensions and measures they
    may see. Labels/synonyms/descriptions are in the user's language; ``lang=fa|en``
    picks another one and ``lang=all`` returns both."""
    if lang is None:
        lang = active_language()
    return {"semantic_models": catalog(lang=None if lang == "all" else lang, request=request)}


@router.post("/validate")
def validate_spec(request, spec: ReportSpec):
    """Check a spec without running it; returns ``valid`` plus structured errors, and
    for a valid spec its resolved calendar/period and interpretation sentence."""
    try:
        report = prepare(spec, request)
    except SpecValidationError as exc:
        return {"valid": False, "errors": [e.as_dict() for e in exc.errors]}
    return {
        "valid": True,
        "errors": [],
        "calendar": report.calendar,
        "period": report.period.as_dict() if report.period else None,
        "interpretation": describe(report),
    }


def _submit(request, spec: ReportSpec, use_cache: bool):
    prepare(spec, request)  # permission and spec errors come back now, not from the job
    if use_cache:
        cached = run(spec, request, cache_only=True)
        if cached is not None:
            return cached.as_dict()
    job = jobs.submit(spec, request, use_cache=use_cache)
    return Status(202, {"job": jobs.payload(job)})


@router.post("/run", response={200: dict, 202: dict})
def run_report(request, spec: ReportSpec, cache: bool = True, mode: RunMode = "auto"):
    """Validate and execute a ``ReportSpec`` (same staff auth + view perms as CRUD).

    ``cache=false`` bypasses the result cache. ``mode=async`` (or ``auto`` when the
    semantic model or ``GOZAR["REPORT_ASYNC"]`` asks for it) queues a background
    job and answers ``202 {"job": …}`` — unless the result is already cached."""
    if spec.semantic_model in semantic_site and jobs.wants_async(
        semantic_site.get(spec.semantic_model), mode
    ):
        return _submit(request, spec, cache)
    return run(spec, request, use_cache=cache).as_dict()


@router.get("/values")
def values(request, semantic_model: str, dimension: str, q: str = ""):
    """Values of a (non-time) dimension for filter pickers; ``q`` searches them."""
    return dimension_values(semantic_model, dimension, q, request)


class DrilldownIn(Schema):
    spec: ReportSpec
    cell: dict[str, Scalar | None] = {}
    measure: str | None = None
    page: int = 1
    page_size: int | None = None
    comparison: bool = False


@router.post("/drilldown")
def drill_down(request, data: DrilldownIn):
    """Records behind one report cell: the spec's filters plus the clicked group
    values (time buckets become their date range) and, when ``measure`` is
    given, that measure's own filter. Paginated, CRUD-style rows. With
    ``comparison``, the records behind the cell's comparison value instead."""
    return drilldown(
        data.spec,
        data.cell,
        data.measure,
        request,
        data.page,
        data.page_size,
        comparison=data.comparison,
    )


def _download(body: bytes, content_type: str, filename: str) -> HttpResponse:
    response = HttpResponse(body, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@router.post("/export")
def export_report(request, spec: ReportSpec, format: ExportFormat = "csv", cache: bool = True):
    """Run ``spec`` and download it as CSV or XLSX (headers in the user's language)."""
    return _download(*export(run(spec, request, use_cache=cache, kind="export"), format))


# --- background jobs -----------------------------------------------------------------


@router.post("/jobs", response={200: dict, 202: dict})
def submit_job(request, spec: ReportSpec, cache: bool = True):
    """Run ``spec`` in the background (always), like ``/run?mode=async``."""
    return _submit(request, spec, cache)


@router.get("/jobs")
def list_jobs(request):
    """The current user's 20 most recent jobs."""
    mine = ReportJob.objects.filter(user=request.user, kind=ReportJob.Kind.REPORT)
    mine = mine.defer("result")[:20]
    return {"jobs": [jobs.payload(j) for j in mine]}


def _job(request, job_id: UUID) -> ReportJob:
    job = ReportJob.objects.filter(pk=job_id, user=request.user, kind=ReportJob.Kind.REPORT).first()
    if job is None:  # someone else's job is indistinguishable from a missing one
        raise ReportError(404, "job_not_found", _("Report job not found"))
    return job


@router.get("/jobs/{job_id}")
def job_status(request, job_id: UUID):
    return jobs.payload(_job(request, job_id))


def _finished_result(job: ReportJob) -> dict:
    if job.status == ReportJob.Status.DONE:
        return job.result
    if job.status == ReportJob.Status.FAILED:
        raise ReportError(
            job.error_status or 500,
            (job.error or {}).get("code") or "invalid_spec",
            (job.error or {}).get("detail") or _("The report failed."),
            **{k: v for k, v in (job.error or {}).items() if k not in ("code", "detail")},
        )
    if job.status == ReportJob.Status.CANCELLED:
        raise ReportError(410, "job_cancelled", _("The report job was cancelled"))
    raise ReportError(
        409, "job_not_finished", _("The report job has not finished"), job=jobs.payload(job)
    )


@router.get("/jobs/{job_id}/result")
def job_result(request, job_id: UUID):
    """The finished job's result — the same body ``/run`` returns — or its error
    with the status code a synchronous run would have had."""
    return _finished_result(_job(request, job_id))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request, job_id: UUID):
    job = _job(request, job_id)
    jobs.cancel(job)
    return jobs.payload(job)


@router.get("/jobs/{job_id}/export")
def export_job(request, job_id: UUID, format: ExportFormat = "csv"):
    """Download a finished job's stored result (no new query)."""
    job = _job(request, job_id)
    with audit.record("export", ReportSpec.model_validate(job.spec), request, job=job) as entry:
        result = _finished_result(job)
        entry.spec, entry.row_count = result["spec"], len(result["rows"])
    body, content_type, filename = export(result, format)
    return _download(body, content_type, filename)
