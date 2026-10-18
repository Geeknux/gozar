"""Validate a ``ReportSpec`` against the semantic catalog.

Every measure, dimension, filter and sort is checked; values are coerced to the
dimension's type; relative periods are resolved. All problems are collected and
raised together as structured ``SpecError``s, so a UI can highlight each one and
the NL layer can feed them back to the LLM for a retry.
"""

from __future__ import annotations

import datetime as dt
import difflib
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings

from bi_admin.calendar import jalali, periods
from bi_admin.discovery import get_registry
from bi_admin.engine.spec import ReportSpec
from bi_admin.semantic import Dimension, Measure, Semantic, semantic_site

DEFAULT_MAX_ROWS = 10_000
DEFAULT_MAX_DIMENSIONS = 6
DEFAULT_GRAIN = "day"

_ARABIC_TO_PERSIAN = str.maketrans({"ي": "ی", "ك": "ک", "ى": "ی", "‌": " "})


def _setting(name: str, default: int) -> int:
    return int((getattr(settings, "GOZAR", {}) or {}).get(name, default))


def max_rows() -> int:
    return _setting("REPORT_MAX_ROWS", DEFAULT_MAX_ROWS)


def max_dimensions() -> int:
    return _setting("REPORT_MAX_DIMENSIONS", DEFAULT_MAX_DIMENSIONS)


@dataclass
class SpecError:
    code: str
    message: str
    path: str
    value: Any = None
    suggestions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if isinstance(out["value"], dt.date):
            out["value"] = out["value"].isoformat()
        return out


class SpecValidationError(Exception):
    def __init__(self, errors: list[SpecError]):
        super().__init__("; ".join(f"{e.path}: {e.message}" for e in errors))
        self.errors = errors

    @property
    def codes(self) -> list[str]:
        return [e.code for e in self.errors]

    def as_dict(self) -> dict[str, Any]:
        return {"detail": "Invalid report spec", "errors": [e.as_dict() for e in self.errors]}


@dataclass(frozen=True)
class GroupKey:
    dimension: Dimension
    grain: str | None = None  # set for time dimensions

    @property
    def key(self) -> str:
        return self.dimension.key


@dataclass(frozen=True)
class ResolvedFilter:
    dimension: Dimension
    op: str
    value: Any


@dataclass
class ValidatedReport:
    semantic: Semantic
    spec: ReportSpec
    measures: list[Measure]
    groups: list[GroupKey]
    filters: list[ResolvedFilter]
    sort: list[tuple[str, str]]
    limit: int
    calendar: str
    time_dimension: Dimension | None = None
    time_grain: str | None = None
    period: periods.Period | None = None

    @property
    def keys(self) -> list[str]:
        return [g.key for g in self.groups] + [m.key for m in self.measures]


def _normalise(s: str) -> str:
    return " ".join(s.translate(_ARABIC_TO_PERSIAN).strip().lower().split())


def _suggest(value: str, items: dict[str, Dimension] | dict[str, Measure]) -> list[str]:
    """Keys whose key/label/synonym matches ``value`` exactly, else close matches."""
    needle = _normalise(str(value))
    exact = []
    names: dict[str, str] = {}
    for key, item in items.items():
        candidates = [key, *item.label.values(), *(s for v in item.synonyms.values() for s in v)]
        for c in candidates:
            names[_normalise(c)] = key
            if _normalise(c) == needle and key not in exact:
                exact.append(key)
    if exact:
        return exact
    close = difflib.get_close_matches(needle, list(names), n=3, cutoff=0.6)
    return list(dict.fromkeys(names[c] for c in close))


class _Validator:
    def __init__(self, spec: ReportSpec, today: dt.date | None):
        self.spec = spec
        self.today = today
        self.errors: list[SpecError] = []

    def error(self, code, message, path, value=None, suggestions=None):
        self.errors.append(SpecError(code, message, path, value, list(suggestions or [])))

    # -- entry --------------------------------------------------------------------

    def run(self) -> ValidatedReport:
        spec = self.spec
        if spec.clarification_needed:
            self.error(
                "clarification_needed",
                spec.clarification_needed,
                "clarification_needed",
                spec.clarification_needed,
            )
        if spec.semantic_model not in semantic_site:
            names = semantic_site.names()
            self.error(
                "unknown_semantic_model",
                f"Unknown semantic model {spec.semantic_model!r}",
                "semantic_model",
                spec.semantic_model,
                difflib.get_close_matches(spec.semantic_model, names, n=3) or names,
            )
            raise SpecValidationError(self.errors)
        sem = semantic_site.get(spec.semantic_model)
        self.sem = sem
        self.dims = sem.visible_dimensions()
        self.meas = sem.visible_measures()

        measures = self.measures()
        time_dim = self.time_dimension()
        groups = self.groups(time_dim)
        period = self.period(time_dim)
        filters = self.filters()
        sort = self.sort(groups, measures)
        limit = self.limit()
        if spec.compare_to is not None:
            self.error(
                "not_supported",
                "compare_to is not supported yet (planned for M6)",
                "compare_to",
                spec.compare_to,
            )
        if self.errors:
            raise SpecValidationError(self.errors)
        return ValidatedReport(
            semantic=sem,
            spec=spec,
            measures=measures,
            groups=groups,
            filters=filters,
            sort=sort,
            limit=limit,
            calendar=spec.calendar,
            time_dimension=time_dim,
            time_grain=next((g.grain for g in groups if g.dimension is time_dim), None),
            period=period,
        )

    # -- parts --------------------------------------------------------------------

    def lookup_dimension(self, key: str, path: str) -> Dimension | None:
        dim = self.dims.get(key)
        if dim is None:
            if key in self.meas:
                self.error(
                    "not_a_dimension", f"{key!r} is a measure, not a dimension", path, key, [key]
                )
            else:
                self.error(
                    "unknown_dimension",
                    f"Unknown dimension {key!r} on {self.sem.name}",
                    path,
                    key,
                    _suggest(key, self.dims),
                )
            return None
        if dim.path is not None:
            try:  # the registry is the source of truth for what paths exist
                get_registry().resolve_lookup(self.sem.model_label, dim.path)
            except LookupError as exc:
                self.error("invalid_lookup", str(exc), path, key)
                return None
        return dim

    def measures(self) -> list[Measure]:
        if not self.spec.measures:
            self.error(
                "no_measures", "Pick at least one measure", "measures", None, list(self.meas)
            )
        out: list[Measure] = []
        seen = set()
        for i, key in enumerate(self.spec.measures):
            path = f"measures[{i}]"
            if key in seen:
                self.error("duplicate", f"Measure {key!r} is listed twice", path, key)
                continue
            seen.add(key)
            m = self.meas.get(key)
            if m is None:
                if key in self.dims:
                    self.error(
                        "not_a_measure", f"{key!r} is a dimension, not a measure", path, key, [key]
                    )
                else:
                    self.error(
                        "unknown_measure",
                        f"Unknown measure {key!r} on {self.sem.name}",
                        path,
                        key,
                        _suggest(key, self.meas),
                    )
                continue
            out.append(m)
        return out

    def time_dimension(self) -> Dimension | None:
        spec = self.spec
        key = spec.time_dimension
        if key is None:
            key = self.sem.time_dimension
            if key is None:
                if spec.time_grain or spec.relative_period:
                    self.error(
                        "no_time_dimension",
                        f"{self.sem.name} has no time dimension",
                        "time_dimension",
                    )
                return None
        dim = self.lookup_dimension(key, "time_dimension")
        if dim is not None and dim.type != "time":
            self.error(
                "not_a_time_dimension",
                f"{key!r} is not a time dimension",
                "time_dimension",
                key,
                [k for k, d in self.dims.items() if d.type == "time"],
            )
            return None
        return dim

    def groups(self, time_dim: Dimension | None) -> list[GroupKey]:
        spec = self.spec
        grain = spec.time_grain
        groups: list[GroupKey] = []
        seen: set[str] = set()
        for i, key in enumerate(spec.dimensions):
            path = f"dimensions[{i}]"
            if key in seen:
                self.error("duplicate", f"Dimension {key!r} is listed twice", path, key)
                continue
            seen.add(key)
            dim = self.lookup_dimension(key, path)
            if dim is None:
                continue
            groups.append(GroupKey(dim, (grain or DEFAULT_GRAIN) if dim.type == "time" else None))
        if grain and time_dim is not None and time_dim.key not in seen:
            groups.insert(0, GroupKey(time_dim, grain))
        if len(groups) > max_dimensions():
            self.error(
                "too_many_dimensions",
                f"At most {max_dimensions()} dimensions per report",
                "dimensions",
                len(groups),
            )
        return groups

    def period(self, time_dim: Dimension | None) -> periods.Period | None:
        name = self.spec.relative_period
        if not name:
            return None
        try:
            period = periods.resolve(name, self.spec.calendar, self.today)
        except periods.PeriodError as exc:
            self.error(
                "unknown_relative_period",
                str(exc),
                "relative_period",
                name,
                list(periods.EXAMPLES),
            )
            return None
        return period if time_dim is not None else None

    def filters(self) -> list[ResolvedFilter]:
        out = []
        for i, f in enumerate(self.spec.filters):
            path = f"filters[{i}]"
            if f.field in self.meas and f.field not in self.dims:
                self.error(
                    "measure_filter_not_supported",
                    f"Filtering on measure {f.field!r} is not supported; filter on a dimension",
                    f"{path}.field",
                    f.field,
                )
                continue
            dim = self.lookup_dimension(f.field, f"{path}.field")
            if dim is None:
                continue
            value = self.filter_value(dim, f.op, f.value, path)
            if value is not _INVALID:
                out.append(ResolvedFilter(dim, f.op, value))
        return out

    def filter_value(self, dim: Dimension, op: str, value: Any, path: str) -> Any:
        vpath = f"{path}.value"
        if op == "is_null":
            if value is None:
                return True
            if isinstance(value, bool):
                return value
            self.error("invalid_value", "is_null takes true/false", vpath, value)
            return _INVALID
        if op in ("gte", "lte", "between") and dim.type not in ("time", "number"):
            self.error(
                "invalid_operator",
                f"{op!r} needs a time or number dimension; {dim.key!r} is {dim.type}",
                f"{path}.op",
                op,
                ["eq", "neq", "in", "not_in", "contains", "is_null"],
            )
            return _INVALID
        if op == "contains" and dim.type not in ("category", "geo"):
            self.error(
                "invalid_operator",
                f"'contains' needs a text dimension; {dim.key!r} is {dim.type}",
                f"{path}.op",
                op,
            )
            return _INVALID
        if op in ("in", "not_in", "between"):
            if not isinstance(value, list) or not value:
                self.error("invalid_value", f"{op!r} takes a non-empty list", vpath, value)
                return _INVALID
            if op == "between" and len(value) != 2:
                self.error("invalid_value", "'between' takes [low, high]", vpath, value)
                return _INVALID
            coerced = [self.coerce(dim, v, f"{vpath}[{j}]") for j, v in enumerate(value)]
            if any(c is _INVALID for c in coerced):
                return _INVALID
            if op == "between" and coerced[0] > coerced[1]:
                self.error("invalid_value", "'between' low is greater than high", vpath, value)
                return _INVALID
            return coerced
        if isinstance(value, list) or value is None:
            self.error("invalid_value", f"{op!r} takes a single value", vpath, value)
            return _INVALID
        if op == "contains":
            return str(value)
        return self.coerce(dim, value, vpath)

    def coerce(self, dim: Dimension, value: Any, path: str) -> Any:
        if isinstance(value, list) or value is None:
            self.error("invalid_value", "Expected a single value", path, value)
            return _INVALID
        if dim.type == "time":
            try:
                return jalali.parse_date(value)
            except ValueError:
                self.error(
                    "invalid_value",
                    "Expected a date: 2026-03-21 (Gregorian) or 1405/01/01 (Jalali)",
                    path,
                    value,
                )
                return _INVALID
        if dim.type == "number":
            if isinstance(value, bool):
                self.error("invalid_value", "Expected a number", path, value)
                return _INVALID
            try:
                return Decimal(str(value).translate(jalali.PERSIAN_DIGITS))
            except InvalidOperation:
                self.error("invalid_value", "Expected a number", path, value)
                return _INVALID
        if dim.type == "boolean":
            if isinstance(value, bool):
                return value
            low = str(value).strip().lower()
            if low in ("1", "true", "yes"):
                return True
            if low in ("0", "false", "no"):
                return False
            self.error("invalid_value", "Expected true or false", path, value)
            return _INVALID
        if dim.choices:
            values = {str(v): v for v, _ in dim.choices}
            if str(value) in values:
                return values[str(value)]
            by_label = {_normalise(label): v for v, label in dim.choices}
            if _normalise(str(value)) in by_label:
                return by_label[_normalise(str(value))]
            self.error(
                "invalid_choice",
                f"{value!r} is not a valid {dim.key}",
                path,
                value,
                [str(v) for v, _ in dim.choices],
            )
            return _INVALID
        return value

    def sort(self, groups: list[GroupKey], measures: list[Measure]) -> list[tuple[str, str]]:
        selected = {g.key for g in groups} | {m.key for m in measures}
        out = []
        for i, s in enumerate(self.spec.sort):
            path = f"sort[{i}].field"
            if s.field not in selected:
                known = s.field in self.dims or s.field in self.meas
                self.error(
                    "sort_field_not_in_report" if known else "unknown_field",
                    f"Sort field {s.field!r} must be one of the report's measures or dimensions",
                    path,
                    s.field,
                    sorted(selected),
                )
                continue
            out.append((s.field, s.direction))
        return out

    def limit(self) -> int:
        cap = max_rows()
        limit = self.spec.limit
        if limit is None:
            return cap
        if limit < 1:
            self.error("invalid_limit", "limit must be at least 1", "limit", limit)
        elif limit > cap:
            self.error("limit_exceeded", f"limit may not exceed {cap}", "limit", limit)
        return min(max(limit, 1), cap)


_INVALID = object()


def validate(spec: ReportSpec | dict, today: dt.date | None = None) -> ValidatedReport:
    """Validate ``spec``; raises ``SpecValidationError`` listing every problem."""
    if isinstance(spec, dict):
        spec = ReportSpec.model_validate(spec)
    return _Validator(spec, today).run()
