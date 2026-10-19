"""Natural-language endpoints: ask a question, get a validated ``ReportSpec`` (and,
optionally, its result), describe a spec in words. Same staff session + CSRF,
permissions, visibility, row rules and limits as ``/reports``."""

from ninja import Router, Schema
from pydantic import Field

from bi_admin.engine import ReportSpec, run
from bi_admin.engine.runner import prepare
from bi_admin.execution import jobs
from bi_admin.nl.catalog import visible_catalog
from bi_admin.nl.conf import nl_setting
from bi_admin.nl.interpret import describe
from bi_admin.nl.pipeline import ask
from bi_admin.nl.providers import get_provider, llm_configured
from bi_admin.nl.text import is_persian
from bi_admin.semantic import semantic_site
from bi_admin.semantic.base import active_language

router = Router(tags=["nl"])


class AskIn(Schema):
    question: str = Field(max_length=4000)
    previous: ReportSpec | None = None  # the spec being refined ("now only Tehran")
    execute: bool = False  # also run the spec (or queue it, like /reports/run)


class InterpretIn(Schema):
    spec: ReportSpec


@router.get("/status")
def nl_status(request):
    """Which interpreter answers, and example questions in the user's language."""
    entries = visible_catalog(request)
    lang = active_language()
    examples = [
        ex["question"]
        for entry in entries
        for ex in entry.examples
        if is_persian(ex["question"]) == (lang == "fa")
    ]
    try:
        provider = get_provider().describe()
    except Exception:  # misconfigured: /ask falls back or reports the error
        provider = {"name": str(nl_setting("PROVIDER")), "model": ""}
    return {
        "enabled": bool(entries),
        "provider": {"name": provider.get("name"), "model": provider.get("model")},
        "llm": llm_configured() and provider.get("name") != "heuristic",
        "max_length": int(nl_setting("MAX_QUESTION_LENGTH") or 0),
        "examples": examples[:6],
    }


@router.post("/ask")
def nl_ask(request, data: AskIn):
    """Interpret ``question`` (English or Persian) as a report.

    ``status`` is ``ok`` (``spec`` + ``interpretation``), ``clarification``
    (``clarification`` asks the user; ``suggestions`` are ready-made follow-ups)
    or ``invalid`` (no valid spec after the retries). With ``execute``, an ``ok``
    answer also carries ``result`` (the ``/reports/run`` body) or ``job`` (a
    queued background job)."""
    answer = ask(data.question, request, previous=data.previous, rate_limit=True)
    body = answer.as_dict()
    if data.execute and answer.status == "ok":
        spec = ReportSpec.model_validate(answer.spec)
        sem = semantic_site.get(spec.semantic_model)
        if jobs.wants_async(sem, "auto"):
            cached = run(spec, request, cache_only=True)
            if cached is not None:
                body["result"] = cached.as_dict()
            else:
                body["job"] = jobs.payload(jobs.submit(spec, request))
        else:
            body["result"] = run(spec, request).as_dict()
    return body


@router.post("/interpret")
def nl_interpret(request, data: InterpretIn):
    """The interpretation sentence of a spec (422 when it isn't valid)."""
    report = prepare(data.spec, request)
    return {
        "interpretation": describe(report, active_language()),
        "period": report.period.as_dict() if report.period else None,
    }
