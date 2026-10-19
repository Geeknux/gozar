"""The LLM prompt: instructions, today's date in both calendars, the retrieved
catalog subset, few-shot examples, the previous spec (refinement) and, on a
retry, the validation errors of the last answer.

What goes in is metadata and the user's own words only: catalog keys, labels,
synonyms, descriptions, types and declared choices; never rows, never values
read from the database (category values in a previous spec are replaced by
placeholders before they get here — see ``pipeline.redact``).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

from bi_admin.calendar import jalali, periods
from bi_admin.engine.spec import ReportSpec

if TYPE_CHECKING:
    from bi_admin.nl.providers import Task

INSTRUCTIONS = """\
You turn a business question into a report spec for Gozar, a reporting tool.
Answer with ONE JSON object matching the ReportSpec schema and nothing else.

Rules:
- Use only semantic_model, measure and dimension keys listed in CATALOG. Never invent keys.
- measures: what to compute. dimensions: what to group by (not time grain).
- Time: set time_grain (day|week|month|season|year) for "monthly", "by month", "ماهانه"...;
  time_dimension only when the question names a date field other than the default.
- Relative periods go in relative_period as a NAME; never compute dates yourself.
  Grammar: today | yesterday | this_<unit> | last_<unit> | last_<N>_<unit>s | past_<N>_<unit>s
  with unit in day, week, month, season, year. "last 3 months" -> "last_3_months",
  "this year"/"امسال" -> "this_year", "last year"/"پارسال" -> "last_year",
  "this season/quarter" -> "this_season". last_* = complete units before the current one;
  past_* = the current unit to date plus the N-1 before it.
- Explicit dates: a filter {"field": <time dimension>, "op": "between", "value": [from, to]}
  with the dates exactly as written (YYYY/MM/DD Jalali or YYYY-MM-DD Gregorian). A whole year
  or month may be written as "1404" or "1404/03" (or "2025", "2025-03"); code expands it.
- Category filters: op eq/neq/in/not_in with the value AS THE USER WROTE IT (any language or
  spelling); the server matches it to stored values. For dimensions with "choices" use a
  choice value. Values like "@v1" are placeholders: copy them unchanged.
- Number filters: gte/lte/between with plain numbers (no thousands separators).
- "top N"/"N بیشترین" -> sort by the measure desc and limit N; "lowest" -> asc.
- chart: "line" for a time series, "bar" for one category dimension, "pie" only when asked,
  "kpi" for a single number without dimensions, otherwise "table".
- Leave calendar null unless the user asks for Jalali/شمسی or Gregorian/میلادی explicitly.
- If the question is ambiguous (e.g. two measures or date fields fit equally) or not about
  the catalog, set clarification_needed to a short question for the user, in the user's
  language, and still fill semantic_model and measures with your best guess.
- When PREVIOUS SPEC is given, the question refines it: return the whole edited spec,
  keeping everything the user did not ask to change.
"""


def today_context(today: dt.date) -> dict[str, Any]:
    j = jalali.to_jalali(today)
    return {
        "gregorian": today.isoformat(),
        "jalali": f"{j.year:04d}/{j.month:02d}/{j.day:02d}",
        "jalali_month_name": jalali.MONTH_NAMES_FA[j.month - 1],
        "weekday": today.strftime("%A"),
    }


def context(task: Task) -> dict[str, Any]:
    return {
        "TODAY": today_context(task.today),
        "USER": {"language": task.language, "calendar": task.calendar},
        "RELATIVE_PERIOD_EXAMPLES": list(periods.EXAMPLES),
        "CATALOG": [entry.prompt() for entry in task.catalog],
    }


def examples(task: Task) -> list[dict[str, Any]]:
    out = []
    for entry in task.catalog:
        for ex in entry.examples:
            out.append({"question": ex["question"], "spec": ex["spec"]})
    return out[:8]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def build_messages(task: Task) -> list[dict[str, str]]:
    system = INSTRUCTIONS + "\nCONTEXT:\n" + _dump(context(task))
    shots = examples(task)
    if shots:
        system += "\nEXAMPLES:\n" + "\n".join(_dump(s) for s in shots)
    if task.previous:
        user = f"PREVIOUS SPEC:\n{_dump(task.previous)}\nFOLLOW-UP:\n{task.question}"
    else:
        user = task.question
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for attempt in task.attempts:
        messages.append({"role": "assistant", "content": attempt.raw or "(no output)"})
        messages.append(
            {
                "role": "user",
                "content": "That spec is invalid. Fix these errors and answer with the whole "
                "corrected JSON spec only:\n" + _dump(attempt.errors),
            }
        )
    return messages


def response_schema(task: Task) -> dict[str, Any]:
    """ReportSpec's JSON schema with the retrieved semantic models as an enum."""
    schema = ReportSpec.model_json_schema()
    names = [e.name for e in task.catalog]
    if names:
        schema["properties"]["semantic_model"] = {"type": "string", "enum": names}
    return schema
