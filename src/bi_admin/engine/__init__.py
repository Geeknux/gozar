"""Query engine: ``ReportSpec`` → validated → Django ORM → rows + column metadata."""

from bi_admin.engine.compiler import CompiledReport, compile_report
from bi_admin.engine.runner import ReportError, ReportResult, run
from bi_admin.engine.spec import Filter, ReportSpec, Sort
from bi_admin.engine.validator import (
    SpecError,
    SpecValidationError,
    ValidatedReport,
    validate,
)

__all__ = [
    "CompiledReport",
    "Filter",
    "ReportError",
    "ReportResult",
    "ReportSpec",
    "Sort",
    "SpecError",
    "SpecValidationError",
    "ValidatedReport",
    "compile_report",
    "run",
    "validate",
]
