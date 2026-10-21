"""The NL pipeline (DESIGN.md §5.1): question → ReportSpec.

1. Retrieve the catalog entries relevant to the question, from what the user
   may see (semantic-model permissions + M4 dimension/measure visibility).
2. Ask the provider for spec JSON (today's date in both calendars, the user's
   calendar and language, the previous spec when refining).
3. Validate it with the report validator — under the user's permissions — and
   on errors call the provider again with the errors fed back (``MAX_RETRIES``,
   default 2). ``clarification_needed`` stops here and goes back to the user.
4. Match category values against the user's scoped value index (``تهران`` →
   ``Tehran``); an unknown value becomes a clarification with in-scope
   suggestions.
5. Validate the final spec and describe it in the user's language.

Only catalog metadata and the user's text reach the provider: category values
of a previous spec are replaced by placeholders (``@v1``) before the prompt is
built and restored afterwards. Provider results are cached per (question,
retrieved catalog, redacted previous spec, today, calendar, language, provider)
when ``CACHE_TIMEOUT`` > 0; value matching and validation always run fresh,
because they depend on the user's row scope and the data. Every request is
written to ``NLLog``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from django.db import DEFAULT_DB_ALIAS, transaction
from django.utils.module_loading import import_string
from django.utils.translation import gettext as _
from pydantic import ValidationError

from bi_admin import preferences
from bi_admin.calendar import periods
from bi_admin.conf import setting
from bi_admin.engine.errors import ReportError
from bi_admin.engine.runner import prepare
from bi_admin.engine.spec import ReportSpec
from bi_admin.engine.validator import SpecError, SpecValidationError, ValidatedReport
from bi_admin.execution import cache
from bi_admin.models import NLLog
from bi_admin.nl import dates
from bi_admin.nl.catalog import ModelEntry, visible_catalog
from bi_admin.nl.conf import nl_setting
from bi_admin.nl.interpret import describe
from bi_admin.nl.providers import Attempt, Provider, ProviderError, Task, get_provider
from bi_admin.nl.text import normalise
from bi_admin.nl.values import ValueMatcher
from bi_admin.semantic import semantic_site
from bi_admin.semantic.base import active_language

logger = logging.getLogger("bi_admin.nl")

CACHE_PREFIX = "gozar:nl:"
PLACEHOLDER = "@v"


class NLError(ReportError):
    """An NL request refused before any spec was produced (4xx/5xx)."""


@dataclass
class Answer:
    status: str  # "ok" | "clarification" | "invalid"
    question: str
    spec: dict[str, Any] | None = None
    interpretation: str = ""
    clarification: str = ""
    suggestions: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    period: dict[str, Any] | None = None
    provider: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    cached: bool = False
    log_id: int | None = None
    report: ValidatedReport | None = None  # not serialised

    @property
    def retries(self) -> int:
        return max(0, self.attempts - 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "question": self.question,
            "spec": self.spec,
            "interpretation": self.interpretation,
            "clarification": self.clarification,
            "suggestions": self.suggestions,
            "warnings": self.warnings,
            "errors": self.errors,
            "period": self.period,
            "provider": {k: v for k, v in self.provider.items() if k in ("name", "model")},
            "attempts": self.attempts,
            "retries": self.retries,
            "cached": self.cached,
            "log_id": self.log_id,
        }


# --- redaction of previous specs ---------------------------------------------------


def _category_values(entry: ModelEntry | None, key: str) -> bool:
    """Whether filter values on ``key`` are row data (not declared choices/dates/numbers)."""
    if entry is None:
        return True
    item = entry.dimensions.get(key)
    if item is None:
        return True
    return item.type in ("category", "geo") and not item.choices


def redact(spec: dict[str, Any] | None, entry: ModelEntry | None):
    """``(spec with category values replaced by @vN, {"@vN": value})``."""
    if not spec:
        return spec, {}
    mapping: dict[str, Any] = {}
    out = json.loads(json.dumps(spec, default=str))
    for f in out.get("filters") or []:
        if not _category_values(entry, f.get("field", "")) or f.get("op") in ("is_null",):
            continue

        def hide(v):
            if v is None:
                return v
            token = f"{PLACEHOLDER}{len(mapping) + 1}"
            mapping[token] = v
            return token

        value = f.get("value")
        f["value"] = [hide(v) for v in value] if isinstance(value, list) else hide(value)
    return out, mapping


def restore(data: Any, mapping: dict[str, Any]) -> Any:
    if not mapping:
        return data
    if isinstance(data, dict):
        return {k: restore(v, mapping) for k, v in data.items()}
    if isinstance(data, list):
        return [restore(v, mapping) for v in data]
    if isinstance(data, str) and data in mapping:
        return mapping[data]
    return data


# --- helpers -----------------------------------------------------------------------


def _schema_errors(exc: ValidationError) -> list[SpecError]:
    out = []
    for err in exc.errors():
        loc = err.get("loc") or ()
        path = ""
        for part in loc:
            path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else str(part))
        out.append(SpecError("schema_error", err.get("msg", "invalid"), path, err.get("input")))
    return out


def _clean(data: dict[str, Any], entries: dict[str, ModelEntry]) -> dict[str, Any]:
    """Harmless normalisations of model output before validation: expand partial
    dates on time dimensions and drop empty optional keys."""
    data = {k: v for k, v in data.items() if v is not None or k == "limit"}
    entry = entries.get(data.get("semantic_model", ""))
    filters = []
    for f in data.get("filters") or []:
        if not isinstance(f, dict):
            filters.append(f)
            continue
        item = entry.dimensions.get(f.get("field", "")) if entry else None
        if item is not None and item.type == "time" and "op" in f:
            f = {**f}
            f["op"], f["value"] = dates.expand_filter(f["op"], f.get("value"))
        filters.append(f)
    if "filters" in data:
        data["filters"] = filters
    return data


def _check_rate(request) -> None:
    limit = int(nl_setting("RATE_LIMIT") or 0)
    user = getattr(request, "user", None)
    if not limit or user is None or not user.is_authenticated:
        return
    window = int(time.time() // 60)
    key = f"gozar:nlrate:{user.pk}:{window}"
    backend = cache.backend()
    backend.add(key, 0, 120)
    try:
        count = backend.incr(key)
    except ValueError:
        backend.set(key, 1, 120)
        count = 1
    if count > limit:
        raise NLError(
            429,
            "rate_limited",
            _("Too many questions; try again in a minute (limit: %(limit)s per minute)")
            % {"limit": limit},
        )


def _cache_key(parts: dict[str, Any]) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return CACHE_PREFIX + hashlib.sha256(raw.encode()).hexdigest()


def _retriever():
    target = nl_setting("RETRIEVER")
    return import_string(target) if isinstance(target, str) else target


# --- value matching ------------------------------------------------------------------


@dataclass
class ValueOutcome:
    spec: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    clarification: str = ""
    suggestions: list[dict[str, str]] = field(default_factory=list)


def match_values(spec: dict[str, Any], request, known: set) -> ValueOutcome:
    """Replace category filter values by the stored values they mean (scoped);
    ``known`` values (restored from the user's own previous spec) are kept."""
    if spec.get("semantic_model") not in semantic_site:
        return ValueOutcome(spec)
    sem = semantic_site.get(spec["semantic_model"])
    matcher = ValueMatcher(sem, request)
    out = ValueOutcome(json.loads(json.dumps(spec, default=str)))
    new_filters = []
    for f in out.spec.get("filters") or []:
        key, op = f.get("field"), f.get("op")
        dim = matcher.dimensions.get(key)
        if dim is None or op not in ("eq", "neq", "in", "not_in") or f.get("value") is None:
            new_filters.append(f)
            continue
        raw = f["value"]
        values = raw if isinstance(raw, list) else [raw]
        resolved = []
        for value in values:
            if value in known:  # restored from the user's own previous spec
                resolved.append(value)
                continue
            found = _resolve_one(matcher, key, value, out)
            if found is None:
                return out
            new_key, new_value = found
            if new_key != key:
                key = new_key
            resolved.append(new_value)
        resolved = list(dict.fromkeys(resolved))
        f = {**f, "field": key}
        f["value"] = resolved if isinstance(raw, list) else resolved[0]
        new_filters.append(f)
    if "filters" in out.spec:
        out.spec["filters"] = new_filters
    return out


def _resolve_one(matcher: ValueMatcher, key: str, value, out: ValueOutcome):
    """``(dimension key, stored value)`` for ``value`` or ``None`` (clarification set)."""
    index = matcher.index(key)
    dim = matcher.dimensions[key]
    label = dim.label.get(active_language()) or key
    best, found = index.resolve(str(value))
    if best is not None:
        if best.how == "fuzzy":  # a spelling correction: say so
            out.warnings.append(
                _("Matched “%(value)s” to %(field)s “%(match)s”.")
                % {"value": value, "field": label, "match": best.label}
            )
        return key, best.value
    # the value may belong to another category dimension (city vs province)
    if not found:
        for other, other_dim in matcher.dimensions.items():
            if other == key:
                continue
            other_best, _alts = matcher.index(other).resolve(str(value))
            if other_best is not None and other_best.how != "fuzzy":
                out.warnings.append(
                    _("“%(value)s” is a %(field)s, so the filter uses %(field)s.")
                    % {
                        "value": value,
                        "field": other_dim.label.get(active_language()) or other,
                    }
                )
                return other, other_best.value
    options = list(dict.fromkeys(m.label for m in found))[:5]
    if options:
        out.clarification = _("No %(field)s matches “%(value)s”. Did you mean %(options)s?") % {
            "field": label,
            "value": value,
            "options": _(", ").join(f"“{o}”" for o in options),
        }
    else:
        out.clarification = _("No %(field)s matches “%(value)s”.") % {
            "field": label,
            "value": value,
        }
    out.suggestions = [{"label": o, "replace": str(value), "with": o} for o in options]
    return None


# --- audit -----------------------------------------------------------------------------


def _log(request, answer: Answer | None, **fields) -> int | None:
    if not setting("AUDIT_REPORTS"):
        return None
    user = getattr(request, "user", None) if request is not None else None
    if user is not None and not user.is_authenticated:
        user = None
    try:
        with transaction.atomic(using=DEFAULT_DB_ALIAS):
            entry = NLLog.objects.using(DEFAULT_DB_ALIAS).create(
                user=user,
                username=user.get_username() if user is not None else "",
                **fields,
            )
        return entry.pk
    except Exception:
        logger.exception("Could not write the NL audit log entry")
        return None


# --- the pipeline ----------------------------------------------------------------------


def ask(
    question: str,
    request=None,
    previous: ReportSpec | dict | None = None,
    today: dt.date | None = None,
    provider: Provider | None = None,
    use_cache: bool = True,
    rate_limit: bool = False,
) -> Answer:
    """Turn ``question`` into a validated ``ReportSpec`` for the user behind ``request``.

    ``previous`` is the spec being refined; ``today`` pins relative dates;
    ``use_cache=False`` always calls the provider; ``rate_limit`` applies
    ``RATE_LIMIT`` (the API does). Raises ``NLError`` (403 no
    reportable data, 422 empty/too long, 429 rate limit, 502 provider failure
    without fallback)."""
    if isinstance(previous, ReportSpec):
        previous = previous.model_dump(mode="json")
    run = _Run((question or "").strip(), request, previous, today, provider, use_cache)
    run.rate_limit = rate_limit
    started = time.monotonic()
    log: dict[str, Any] = {
        "question": run.question[:4000],
        "language": run.language,
        "calendar": run.calendar,
        "previous_spec": previous,
    }
    answer: Answer | None = None
    try:
        answer = run.answer()
    except NLError as exc:
        log.update(
            status=NLLog.Status.DENIED if exc.status in (403, 429) else NLLog.Status.FAILED,
            error_code=exc.code,
            error=str(exc)[:2000],
        )
        raise
    except Exception as exc:
        log.update(status=NLLog.Status.FAILED, error_code="internal_error", error=repr(exc)[:2000])
        raise
    finally:
        if answer is not None:
            log.update(
                status={
                    "ok": NLLog.Status.OK,
                    "clarification": NLLog.Status.CLARIFICATION,
                }.get(answer.status, NLLog.Status.INVALID),
                semantic_model=(answer.spec or {}).get("semantic_model", "")[:100],
                spec=answer.spec,
                interpretation=answer.interpretation,
                clarification=answer.clarification,
                provider=answer.provider.get("name", "")[:50],
                model_name=str(answer.provider.get("model", ""))[:200],
                attempts=answer.attempts,
                cache_hit=answer.cached,
                error_code=answer.errors[0]["code"][:64] if answer.errors else "",
            )
        if nl_setting("LOG_PROMPTS"):
            log["transcript"] = run.transcript
        duration = int((time.monotonic() - started) * 1000)
        log_id = _log(request, answer, duration_ms=duration, **log)
        if answer is not None:
            answer.log_id = log_id
    return answer


class _Run:
    """One NL request: the user's context and what happened so far."""

    def __init__(self, question, request, previous, today, provider, use_cache):
        self.question = question
        self.request = request
        self.previous = previous
        self.today = today or periods.today()
        self.provider = provider
        self.use_cache = use_cache
        self.language = active_language()
        self.calendar = (
            preferences.get(request).calendar
            if request is not None
            else preferences.default_calendar()
        )
        self.transcript: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.entries: list[ModelEntry] = []
        self.rate_limit = False

    def answer(self) -> Answer:
        question, request = self.question, self.request
        if not question:
            raise NLError(422, "empty_question", _("Type a question"))
        max_len = int(nl_setting("MAX_QUESTION_LENGTH") or 0)
        if max_len and len(question) > max_len:
            raise NLError(
                422,
                "question_too_long",
                _("Questions are limited to %(max)s characters") % {"max": max_len},
            )
        self.entries = visible_catalog(request)
        if not any(e.measures for e in self.entries):
            raise NLError(403, "permission_denied", _("You don't have access to any report data"))
        if self.rate_limit:
            _check_rate(request)
        by_name = {e.name: e for e in self.entries}
        provider = self.provider or get_provider()

        previous = self.previous
        if previous and previous.get("semantic_model") not in by_name:
            previous = None  # a model the user can't see (or a stale spec): start over
        redacted, mapping = redact(previous, by_name.get((previous or {}).get("semantic_model")))

        answer = Answer("invalid", question)
        answer.warnings = self.warnings
        data, attempts, cached, used = self.generate(provider, redacted)
        answer.provider, answer.attempts, answer.cached = used.describe(), attempts, cached
        self.warnings += data.get("notes") or []
        if data is None or "errors" in data:
            answer.errors = (data or {}).get("errors", [])
            answer.clarification = _(
                "Sorry, I couldn't turn that into a report. Try naming what to measure and "
                "how to group it, e.g. “revenue by city last month”."
            )
            return answer
        spec_data = restore(data["spec"], mapping)
        if data.get("clarification"):
            answer.status = "clarification"
            answer.clarification = data["clarification"]
            answer.spec = spec_data if spec_data.get("measures") else None
            return answer

        outcome = match_values(spec_data, request, set(mapping.values()))
        self.warnings += outcome.warnings
        if outcome.clarification:
            answer.status = "clarification"
            answer.clarification = outcome.clarification
            answer.suggestions = [
                {
                    "label": s["label"],
                    "question": question.replace(s["replace"], s["with"])
                    if s["replace"] in question
                    else f"{question} {s['with']}",
                }
                for s in outcome.suggestions
            ]
            answer.spec = outcome.spec
            return answer
        try:
            spec = ReportSpec.model_validate(outcome.spec)
            report = prepare(spec, request, self.today)
        except (SpecValidationError, ValidationError) as exc:
            errors = exc.errors if isinstance(exc, SpecValidationError) else _schema_errors(exc)
            answer.errors = [e.as_dict() for e in errors]
            answer.spec = outcome.spec
            answer.clarification = _("The matched report is not valid: %(error)s") % {
                "error": errors[0].message
            }
            return answer
        answer.status = "ok"
        answer.spec = spec.model_dump(mode="json")
        answer.report = report
        answer.interpretation = describe(report, self.language)
        answer.period = report.period.as_dict() if report.period else None
        return answer

    def task(self, provider: Provider, catalog: list[ModelEntry], redacted) -> Task:
        task = Task(
            question=self.question,
            language=self.language,
            calendar=self.calendar,
            today=self.today,
            catalog=catalog,
            previous=redacted,
        )
        if provider.local:  # in-process: may read the (scoped) value index
            matchers: dict[str, ValueMatcher] = {}

            def values(name: str):
                if name not in matchers and name in semantic_site:
                    matchers[name] = ValueMatcher(semantic_site.get(name), self.request)
                return matchers.get(name)

            task.values = values
        return task

    def cache_key(self, provider: Provider, catalog: list[ModelEntry], redacted) -> str | None:
        if not self.use_cache or int(nl_setting("CACHE_TIMEOUT") or 0) <= 0:
            return None
        return _cache_key(
            {
                "question": normalise(self.question),
                "catalog": [e.prompt() for e in catalog],
                "previous": redacted,
                "today": self.today.isoformat(),
                "calendar": self.calendar,
                "language": self.language,
                "provider": provider.describe(),
                "retries": nl_setting("MAX_RETRIES"),
            }
        )

    def generate(self, provider: Provider, redacted):
        """``({"spec", "clarification"} | {"errors"}, attempts, cached, provider used)``."""
        retrieve = _retriever()
        catalog = retrieve(
            self.question,
            self.entries,
            previous=redacted,
            top_k=None if provider.local else int(nl_setting("TOP_K") or 0) or None,
            max_models=len(self.entries) if provider.local else int(nl_setting("MAX_MODELS") or 3),
        )
        key = self.cache_key(provider, catalog, redacted)
        if key is not None:
            hit = cache.backend().get(key)
            if hit is not None:
                return hit["data"], hit["attempts"], True, provider

        by_name = {e.name: e for e in self.entries}
        task = self.task(provider, catalog, redacted)
        max_retries = int(nl_setting("MAX_RETRIES") or 0)
        data: dict[str, Any] = {"errors": []}
        attempts = 0
        while attempts <= max_retries:
            attempts += 1
            try:
                gen = provider.generate(task)
            except ProviderError as exc:
                logger.warning("NL provider %s failed: %s", provider.name, exc)
                self.transcript.append({"attempt": attempts, "error": exc.code, "detail": str(exc)})
                fallback = bool(nl_setting("FALLBACK") and not provider.local)
                from bi_admin.alerts import hooks

                hooks.nl_provider_error(provider.name, exc.code, fallback)
                if fallback:
                    from bi_admin.nl.heuristic import HeuristicProvider

                    self.warnings.append(
                        _("The language model is unavailable; the built-in interpreter answered.")
                    )
                    return self.generate(HeuristicProvider(), redacted)
                raise NLError(
                    502,
                    exc.code,
                    _("The language model could not be reached: %(error)s") % {"error": exc},
                ) from None
            self.transcript.append(
                {"attempt": attempts, "messages": gen.messages, "output": gen.raw}
            )
            errors = self.check(gen, by_name)
            if isinstance(errors, dict):
                data = {**errors, "notes": gen.notes}
                break
            data = {"errors": [e.as_dict() for e in errors]}
            task.attempts.append(Attempt(gen.raw, data["errors"]))
        if key is not None and "errors" not in data:
            cache.backend().set(
                key, {"data": data, "attempts": attempts}, int(nl_setting("CACHE_TIMEOUT"))
            )
        return data, attempts, False, provider

    def check(self, gen, by_name) -> dict[str, Any] | list[SpecError]:
        """The accepted ``{"spec", "clarification"}`` or the errors to feed back."""
        if gen.data is None:
            return [SpecError("output_not_json", "Answer with one JSON object", "", gen.raw[:200])]
        cleaned = _clean(gen.data, by_name)
        clarification = cleaned.get("clarification_needed")
        if clarification:
            return {"spec": cleaned, "clarification": str(clarification)}
        try:
            spec = ReportSpec.model_validate(cleaned)
        except ValidationError as exc:
            return _schema_errors(exc)
        try:
            # under the user's permissions; placeholders (@v1) are opaque strings,
            # valid wherever a category value is
            prepare(spec, self.request, self.today)
        except SpecValidationError as exc:
            return exc.errors
        except ReportError as exc:  # e.g. a model the user may not see
            return [SpecError(exc.code, str(exc), "semantic_model", spec.semantic_model)]
        return {"spec": spec.model_dump(mode="json"), "clarification": ""}
