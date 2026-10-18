"""Report endpoints: semantic catalog, spec validation and execution."""

from ninja import Router

from bi_admin.engine import ReportSpec, SpecValidationError, run, validate
from bi_admin.engine.runner import authorize
from bi_admin.semantic import catalog

router = Router(tags=["reports"])


@router.get("/catalog")
def report_catalog(request, lang: str | None = None):
    """Semantic models the user may report on, with dimensions and measures.
    ``lang=fa|en`` picks one language for labels/synonyms/descriptions."""
    return {"semantic_models": catalog(lang=lang, request=request)}


@router.post("/validate")
def validate_spec(request, spec: ReportSpec):
    """Check a spec without running it; returns ``valid`` plus structured errors."""
    try:
        authorize(spec, request)
        report = validate(spec)
    except SpecValidationError as exc:
        return {"valid": False, "errors": [e.as_dict() for e in exc.errors]}
    return {
        "valid": True,
        "errors": [],
        "period": report.period.as_dict() if report.period else None,
    }


@router.post("/run")
def run_report(request, spec: ReportSpec):
    """Validate and execute a ``ReportSpec`` (same staff auth + view perms as CRUD)."""
    return run(spec, request).as_dict()
