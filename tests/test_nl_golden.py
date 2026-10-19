"""M5: the NL golden set (tests/nl_golden) scored in CI against the offline
heuristic and the stub provider; ``manage.py gozar_nl_eval`` runs the same cases
against a real model."""

from pathlib import Path

import pytest
from django.test import RequestFactory

from bi_admin.nl import evaluation
from bi_admin.nl.heuristic import HeuristicProvider
from bi_admin.nl.providers import StubProvider

pytestmark = pytest.mark.django_db

GOLDEN = Path(__file__).parent / "nl_golden"


@pytest.fixture
def cases():
    return evaluation.load(GOLDEN)


@pytest.fixture
def request_as(admin_user):
    request = RequestFactory().get("/")
    request.user = admin_user
    return request


def test_golden_set_size(cases):
    persian = [c for c in cases if c.id.startswith("fa-")]
    english = [c for c in cases if c.id.startswith("en-")]
    assert len(persian) >= 100
    assert len(english) >= 20
    assert any(c.previous for c in cases)
    assert any(c.expects_clarification for c in cases)


def test_heuristic_on_golden_set(seeded, cases, request_as):
    report = evaluation.evaluate(cases, request_as, HeuristicProvider())
    failures = [o.as_dict() for o in report.failures()]
    metrics = report.metrics
    print(metrics)
    for f in failures:
        got = {k: v for k, v in (f["got"] or {}).items() if v not in (None, [], 1000)}
        print(f["id"], f["question"], f["status"], "\n  want", f["expected"], "\n  got ", got)
    assert metrics["semantic_match_rate"] >= 0.95, failures
    assert metrics["exact_match_rate"] >= 0.9, failures
    assert metrics["clarification_recall"] == 1.0
    assert metrics["retry_rate"] == 0  # deterministic output is always valid or a question


def _answers(cases, corrupt=False):
    """What a perfect model would answer, in order (optionally wrong once first)."""
    out = []
    for case in cases:
        if case.expects_clarification:
            out.append({**SPEC, "clarification_needed": "Could you be more specific?"})
            continue
        if corrupt:  # an unknown measure the validator rejects; the retry fixes it
            out.append({**case.expected, "measures": ["x_" + m for m in case.expected["measures"]]})
        out.append(case.expected)
    return out


SPEC = {"semantic_model": "order_metrics", "measures": ["revenue"]}


def test_stub_on_golden_set(seeded, cases, request_as):
    """The pipeline itself (validation, value matching, refinement restore) keeps a
    perfect model's answers intact."""
    report = evaluation.evaluate(cases, request_as, StubProvider(_answers(cases)))
    metrics = report.metrics
    assert metrics["exact_match_rate"] == 1.0, [o.as_dict() for o in report.failures()]
    assert metrics["retry_rate"] == 0 and metrics["mean_attempts"] == 1


def test_stub_retry_rate_on_golden_set(seeded, cases, request_as):
    report = evaluation.evaluate(cases, request_as, StubProvider(_answers(cases, corrupt=True)))
    metrics = report.metrics
    answered = [c for c in cases if not c.expects_clarification]
    assert metrics["semantic_match_rate"] == 1.0
    assert metrics["retry_rate"] == round(len(answered) / len(cases), 4)
    assert all(o.attempts == 2 for o in report.outcomes if not o.case.expects_clarification)


def test_scoring_semantics():
    case = evaluation.Case("x", "q", {**SPEC, "dimensions": ["city", "status"], "chart": "table"})
    reordered = {**SPEC, "dimensions": ["status", "city"], "chart": "bar"}
    assert evaluation.score(case, "ok", reordered, evaluation.DEFAULT_TODAY) == (False, True)
    assert evaluation.score(case, "clarification", None, evaluation.DEFAULT_TODAY) == (False, False)
    period = evaluation.Case("p", "q", {**SPEC, "relative_period": "last_3_months"})
    same_range = {**SPEC, "relative_period": "last_3_month"}
    assert evaluation.score(period, "ok", same_range, evaluation.DEFAULT_TODAY) == (False, True)
    values = evaluation.Case(
        "v", "q", {**SPEC, "filters": [{"field": "city", "op": "in", "value": ["Karaj", "Tehran"]}]}
    )
    swapped = {**SPEC, "filters": [{"field": "city", "op": "in", "value": ["Tehran", "Karaj"]}]}
    assert evaluation.score(values, "ok", swapped, evaluation.DEFAULT_TODAY) == (True, True)
    clarify = evaluation.Case("c", "q", None)
    assert evaluation.score(clarify, "clarification", None, evaluation.DEFAULT_TODAY) == (
        True,
        True,
    )


def test_eval_command(seeded, admin_user, tmp_path):
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    report = tmp_path / "report.json"
    call_command(
        "gozar_nl_eval",
        provider="heuristic",
        golden=str(GOLDEN),
        only="en",
        json_path=str(report),
        fail_under=0.9,
        stdout=out,
    )
    assert "semantic_match_rate" in out.getvalue()
    import json

    data = json.loads(report.read_text())
    assert data["provider"]["name"] == "heuristic" and data["metrics"]["cases"] >= 20
