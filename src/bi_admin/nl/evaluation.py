"""Golden-set evaluation of the NL pipeline (DESIGN.md §5.5).

A golden case is ``{"id", "question", "expected": <ReportSpec> | {"clarification": true},
"previous": <ReportSpec>?}`` in a JSON list; every ``*.json`` file of a golden
directory is loaded. Each question goes through the real pipeline (retrieval,
provider, validation + retries, value matching) as a given user, and the
answer is scored:

* **exact** — the normalised specs are identical (``ReportSpec`` defaults filled,
  ``in`` value lists sorted);
* **semantic** — same model, measures, group-by fields (as sets), filters
  (field/op/values, letters and digits normalised), time dimension and grain,
  the same date range for the relative period, and the sort/limit when the
  expected spec has one; the chart type and the calendar are ignored;
* a case expecting a clarification matches when the pipeline asks one.

Metrics: exact and semantic match rate, clarification rate (answers that asked
the user), clarification recall (expected ones that were asked), retry rate
(questions that needed more than one provider call) and the mean number of
calls. ``manage.py gozar_nl_eval`` runs it against any configured provider.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bi_admin.calendar import periods
from bi_admin.engine.spec import ReportSpec
from bi_admin.nl.text import normalise

DEFAULT_TODAY = dt.date(2026, 10, 18)  # the date the golden set was written against


@dataclass
class Case:
    id: str
    question: str
    expected: dict[str, Any] | None  # None: a clarification is expected
    previous: dict[str, Any] | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def expects_clarification(self) -> bool:
        return self.expected is None


def load(path: str | Path) -> list[Case]:
    """Cases from a JSON file or every ``*.json`` file of a directory."""
    path = Path(path)
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    cases = []
    for file in files:
        for raw in json.loads(file.read_text(encoding="utf-8")):
            expected = raw["expected"]
            if expected.get("clarification") is True:
                expected = None
            cases.append(
                Case(
                    id=raw["id"],
                    question=raw["question"],
                    expected=expected,
                    previous=raw.get("previous"),
                    tags=list(raw.get("tags", [])) + [file.stem],
                )
            )
    ids = [c.id for c in cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"Duplicate golden case ids: {', '.join(duplicates)}")
    return cases


def canonical(spec: dict[str, Any]) -> dict[str, Any]:
    """The spec with defaults filled and order-free lists sorted."""
    data = ReportSpec.model_validate(spec).model_dump(mode="json")
    data.pop("clarification_needed", None)
    for f in data["filters"]:
        if f["op"] in ("in", "not_in") and isinstance(f["value"], list):
            f["value"] = sorted(f["value"], key=lambda v: json.dumps(v, ensure_ascii=False))
    data["filters"] = sorted(
        data["filters"], key=lambda f: json.dumps(f, sort_keys=True, ensure_ascii=False)
    )
    return data


def _norm_value(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(sorted(_norm_value(v) for v in value))
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return float(value)
    return normalise(value)


def semantic_view(spec: dict[str, Any], today: dt.date) -> dict[str, Any]:
    data = canonical(spec)
    period = None
    if data["relative_period"]:
        try:
            p = periods.resolve(data["relative_period"], "gregorian", today)
            period = (p.start.isoformat(), p.end.isoformat())
        except periods.PeriodError:
            period = data["relative_period"]
    view = {
        "semantic_model": data["semantic_model"],
        "measures": sorted(data["measures"]),
        "dimensions": sorted(data["dimensions"]),
        "filters": sorted(
            (f["field"], f["op"], json.dumps(_norm_value(f["value"]), ensure_ascii=False))
            for f in data["filters"]
        ),
        "time_dimension": data["time_dimension"],
        "time_grain": data["time_grain"],
        "period": period,
    }
    return view


def _sort_view(spec: dict[str, Any]) -> tuple:
    data = canonical(spec)
    return (
        tuple((s["field"], s["direction"]) for s in data["sort"]),
        data["limit"] if data["sort"] or data["limit"] != 1000 else None,
    )


@dataclass
class Outcome:
    case: Case
    status: str
    spec: dict[str, Any] | None
    exact: bool
    semantic: bool
    attempts: int
    clarification: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.case.id,
            "question": self.case.question,
            "status": self.status,
            "exact": self.exact,
            "semantic": self.semantic,
            "attempts": self.attempts,
            "expected": self.case.expected,
            "got": self.spec,
            "clarification": self.clarification,
            "error": self.error,
        }


def score(case: Case, status: str, spec: dict | None, today: dt.date) -> tuple[bool, bool]:
    if case.expects_clarification:
        ok = status == "clarification"
        return ok, ok
    if status != "ok" or spec is None:
        return False, False
    try:
        exact = canonical(spec) == canonical(case.expected)
        semantic = semantic_view(spec, today) == semantic_view(case.expected, today)
        if semantic and (
            case.expected.get("sort") or case.expected.get("limit") not in (None, 1000)
        ):
            semantic = _sort_view(spec) == _sort_view(case.expected)
    except Exception:
        return False, False
    return exact, semantic


@dataclass
class Report:
    outcomes: list[Outcome]
    provider: dict[str, Any] = field(default_factory=dict)

    @property
    def metrics(self) -> dict[str, Any]:
        n = len(self.outcomes) or 1
        clarified = [o for o in self.outcomes if o.status == "clarification"]
        expected = [o for o in self.outcomes if o.case.expects_clarification]
        return {
            "cases": len(self.outcomes),
            "exact_match_rate": round(sum(o.exact for o in self.outcomes) / n, 4),
            "semantic_match_rate": round(sum(o.semantic for o in self.outcomes) / n, 4),
            "clarification_rate": round(len(clarified) / n, 4),
            "clarification_recall": round(
                sum(o.status == "clarification" for o in expected) / len(expected), 4
            )
            if expected
            else None,
            "retry_rate": round(sum(o.attempts > 1 for o in self.outcomes) / n, 4),
            "mean_attempts": round(sum(o.attempts for o in self.outcomes) / n, 3),
            "errors": sum(bool(o.error) for o in self.outcomes),
        }

    def failures(self) -> list[Outcome]:
        return [o for o in self.outcomes if not o.semantic]

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "metrics": self.metrics,
            "outcomes": [o.as_dict() for o in self.outcomes],
        }


def evaluate(cases: list[Case], request, provider=None, today: dt.date | None = None) -> Report:
    """Run every case through ``bi_admin.nl.pipeline.ask`` as ``request.user``.

    The NL cache is bypassed, so the provider is really called for each case."""
    from bi_admin.engine.errors import ReportError
    from bi_admin.nl.pipeline import ask
    from bi_admin.nl.providers import get_provider

    today = today or DEFAULT_TODAY
    provider = provider or get_provider()
    outcomes = []
    for case in cases:
        try:
            answer = ask(
                case.question,
                request,
                previous=case.previous,
                today=today,
                provider=provider,
                use_cache=False,
            )
        except ReportError as exc:
            outcomes.append(
                Outcome(case, "error", None, False, False, 0, error=f"{exc.code}: {exc}")
            )
            continue
        exact, semantic = score(case, answer.status, answer.spec, today)
        outcomes.append(
            Outcome(
                case,
                answer.status,
                answer.spec,
                exact,
                semantic,
                answer.attempts,
                clarification=answer.clarification,
            )
        )
    return Report(outcomes, provider.describe())
