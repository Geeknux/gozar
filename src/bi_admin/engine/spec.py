"""``ReportSpec``: the structured report request shared by the GUI, API and NL layer.

It is also the LLM output schema (DESIGN.md §3.3), so it forbids unknown keys and
keeps every field a plain JSON type.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FilterOp = Literal["eq", "neq", "in", "not_in", "gte", "lte", "between", "contains", "is_null"]
FILTER_OPS: tuple[str, ...] = FilterOp.__args__  # type: ignore[attr-defined]
TimeGrain = Literal["day", "week", "month", "season", "year"]
CalendarName = Literal["jalali", "gregorian"]
ChartType = Literal["table", "bar", "line", "area", "pie", "pivot", "kpi"]

Scalar = str | int | float | bool


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filter(_Strict):
    field: str = Field(description="dimension key")
    op: FilterOp
    value: Scalar | list[Scalar] | None = None


class Sort(_Strict):
    field: str = Field(description="measure or dimension key")
    direction: Literal["asc", "desc"] = "desc"


class ReportSpec(_Strict):
    semantic_model: str
    measures: list[str]
    dimensions: list[str] = []
    filters: list[Filter] = []
    time_dimension: str | None = Field(
        None, description="time dimension for grain/period; defaults to the model's"
    )
    time_grain: TimeGrain | None = None
    calendar: CalendarName | None = Field(
        None, description="defaults to the user's calendar preference (Gregorian unless set)"
    )
    relative_period: str | None = Field(
        None, description='e.g. "last_3_months", "last_year", "this_season" — resolved in code'
    )
    compare_to: Literal["previous_period", "same_period_last_year"] | None = None
    sort: list[Sort] = []
    limit: int | None = 1000
    chart: ChartType = "table"
    clarification_needed: str | None = Field(
        None, description="set by the LLM when the request is ambiguous"
    )
