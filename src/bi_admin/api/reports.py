"""Report endpoints: semantic catalog, spec validation and execution."""

from typing import Literal

from django.http import HttpResponse
from ninja import Router, Schema

from bi_admin import preferences
from bi_admin.engine import ReportSpec, SpecValidationError, run, validate
from bi_admin.engine.drilldown import drilldown
from bi_admin.engine.export import export
from bi_admin.engine.runner import authorize
from bi_admin.engine.spec import Scalar
from bi_admin.engine.values import dimension_values
from bi_admin.semantic import catalog
from bi_admin.semantic.base import active_language

router = Router(tags=["reports"])


@router.get("/catalog")
def report_catalog(request, lang: str | None = None):
    """Semantic models the user may report on, with dimensions and measures.
    Labels/synonyms/descriptions are in the user's language; ``lang=fa|en``
    picks another one and ``lang=all`` returns both."""
    if lang is None:
        lang = active_language()
    return {"semantic_models": catalog(lang=None if lang == "all" else lang, request=request)}


@router.post("/validate")
def validate_spec(request, spec: ReportSpec):
    """Check a spec without running it; returns ``valid`` plus structured errors."""
    try:
        authorize(spec, request)
        report = validate(spec, calendar=preferences.get(request).calendar)
    except SpecValidationError as exc:
        return {"valid": False, "errors": [e.as_dict() for e in exc.errors]}
    return {
        "valid": True,
        "errors": [],
        "calendar": report.calendar,
        "period": report.period.as_dict() if report.period else None,
    }


@router.post("/run")
def run_report(request, spec: ReportSpec):
    """Validate and execute a ``ReportSpec`` (same staff auth + view perms as CRUD)."""
    return run(spec, request).as_dict()


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


@router.post("/drilldown")
def drill_down(request, data: DrilldownIn):
    """Records behind one report cell: the spec's filters plus the clicked group
    values (time buckets become their date range) and, when ``measure`` is
    given, that measure's own filter. Paginated, CRUD-style rows."""
    return drilldown(data.spec, data.cell, data.measure, request, data.page, data.page_size)


@router.post("/export")
def export_report(request, spec: ReportSpec, format: Literal["csv", "xlsx"] = "csv"):
    """Run ``spec`` and download it as CSV or XLSX (headers in the user's language)."""
    body, content_type, filename = export(run(spec, request), format)
    response = HttpResponse(body, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
