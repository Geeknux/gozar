"""Choose and run the engine for a validated report: a matching pre-aggregation,
then the semantic model's columnar engine, then the ORM.

Every alternative to the ORM either answers exactly what the ORM would, or steps
aside with a reason (``considered`` in the response and the audit log); an error
inside it is logged and the ORM answers. A caller may force an engine
(``run(..., engine="preaggregation")``, used by tests and benchmarks): then a
refusal is a ``409 engine_unavailable`` instead of a fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from django.db import DatabaseError, transaction
from django.utils.translation import gettext as _

from bi_admin.conf import setting
from bi_admin.engine.errors import ReportError
from bi_admin.scale import columnar, matcher
from bi_admin.scale import state as states

logger = logging.getLogger("bi_admin.scale")
ENGINES = ("auto", "orm", "preaggregation")


@dataclass
class Execution:
    engine: str = "orm"
    pre_aggregation: str | None = None
    stale: bool = False
    stale_since: str | None = None
    lag_seconds: float | None = None
    considered: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "pre_aggregation": self.pre_aggregation,
            "stale": self.stale,
            "stale_since": self.stale_since,
            "lag_seconds": self.lag_seconds,
            "considered": self.considered,
        }


def reports_of(report) -> list:
    from bi_admin.engine.runner import comparison_report

    return [report] + ([comparison_report(report)] if report.comparison is not None else [])


def unavailable(engine: str, considered: list[dict]) -> ReportError:
    return ReportError(
        409,
        "engine_unavailable",
        _("The %(engine)s engine can't answer this report") % {"engine": engine},
        considered=considered,
    )


def execute(report, compiled, request, alias: str, engine: str = "auto"):
    """``(rows, truncated, Execution)`` for ``report`` (``compiled`` is its ORM form)."""
    from bi_admin.engine.runner import execute as run_compiled
    from bi_admin.engine.runner import merge_comparison

    info = Execution()
    reports = reports_of(report)
    allowed = engine in ("auto", "preaggregation")
    if allowed and (engine == "preaggregation" or setting("PREAGGREGATIONS")):
        match, refusals = matcher.find(reports, request)
        info.considered += [r.as_dict() for r in refusals]
        if match is not None:
            try:
                with transaction.atomic(using=alias):  # a savepoint: fall back on errors
                    results = [run_preaggregation(r, match, request, alias) for r in reports]
            except DatabaseError as exc:
                logger.exception("Pre-aggregation %s failed; using the ORM", match.preagg.key)
                info.considered.append(
                    matcher.Refusal(
                        "preaggregation", "error", str(exc), match.preagg.name
                    ).as_dict()
                )
            else:
                states.record_hit(match.row)
                info.engine, info.pre_aggregation = "preaggregation", match.preagg.name
                info.stale = not match.freshness.fresh
                fresh = match.freshness.as_dict()
                info.stale_since, info.lag_seconds = fresh["stale_since"], fresh["lag_seconds"]
                return finish(report, results, merge_comparison) + (info,)
        if engine == "preaggregation":
            raise unavailable(engine, info.considered)

    backend = None
    if engine == "auto":
        backend = columnar.backend_for(report.semantic)
    elif engine not in ENGINES:
        backend = columnar.get(engine)
        if backend is None:
            raise unavailable(engine, info.considered)
    if backend is not None:
        try:
            results, meta = backend.execute(reports, request)
        except columnar.Unsupported as exc:
            info.considered.append({"engine": backend.name, "code": exc.code, "detail": exc.detail})
            if engine == backend.name:
                raise unavailable(engine, info.considered) from None
        else:
            info.engine = backend.name
            info.stale = bool(meta.get("stale"))
            info.stale_since, info.lag_seconds = meta.get("stale_since"), meta.get("lag_seconds")
            return finish(report, results, merge_comparison) + (info,)

    rows, truncated = run_compiled(compiled, alias)
    if report.comparison is not None:
        from bi_admin.engine.compiler import compile_report

        previous = compile_report(reports[1], request)
        previous_rows, _truncated = run_compiled(previous, alias)
        merge_comparison(report, rows, previous_rows)
    return rows, truncated, info


def run_preaggregation(report, match: matcher.Match, request, alias: str):
    from bi_admin.engine.compiler import compile_report
    from bi_admin.engine.runner import execute as run_compiled

    model = match.source.model
    shadow = matcher.retarget(report, match.preagg, model)
    return run_compiled(compile_report(shadow, request, source=match.source), alias)


def finish(report, results, merge_comparison) -> tuple[list[dict], bool]:
    rows, truncated = results[0]
    if report.comparison is not None:
        merge_comparison(report, rows, results[1][0])
    return rows, truncated
