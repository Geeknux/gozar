"""Declarative pre-aggregations: summary tables Gozar builds and answers reports from.

Declared on a semantic model::

    @semantic.register
    class OrderMetrics(semantic.Semantic):
        ...
        pre_aggregations = [
            PreAggregation(
                "daily_sales",
                measures=["revenue", "orders", "aov"],
                dimensions=["city", "status"],
                time_grain="day",
                scope_fields=["customer__city__name_en"],  # what the row rules read
            ),
        ]

A pre-aggregation stores, per time bucket and combination of dimension values,
the additive parts of its measures (``bi_admin.scale.additive``). A report is
answered from it only when that is provably the same as running it on the base
rows (``bi_admin.scale.matcher``); otherwise the ORM path runs as before.

Each pre-aggregation is one table, created and dropped by Gozar with Django's
schema editor (no migrations). Its name carries a hash of the definition and a
generation number — ``gozar_pa_<semantic>_<name>_<hash>_<generation>`` — so a
changed definition is never read before it has been rebuilt, and a rebuild fills
a new table before switching to it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from django.apps.registry import Apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models import F

from bi_admin.scale import additive, paths
from bi_admin.semantic.base import CALENDARS, TIME_GRAINS, Text, TextInput, text

FORMAT_VERSION = 1
TABLE_PREFIX = "gozar_pa_"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
TIME_COLUMN = "t_bucket"

# Summary-table models live in their own registry: Django's (migrations, system
# checks, discovery, signals) never sees them. One model per table and definition.
APPS = Apps(installed_apps=())
_MODELS: dict[tuple[str, str], type[models.Model]] = {}


def seconds(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    return float(value)


@dataclass
class Column:
    name: str
    role: str  # time | dimension | scope | part
    field: models.Field
    key: str = ""  # dimension key, scope path or "" for parts
    path: str | None = None  # canonical lookup path (dimension paths and scope fields)


@dataclass
class Grain:
    grain: str
    calendar: str | None  # None: day buckets, valid in every calendar

    # Coarser grains each grain's buckets nest in (same calendar).
    NESTS = {
        "day": ("day", "week", "month", "season", "year"),
        "week": ("week",),
        "month": ("month", "season", "year"),
        "season": ("season", "year"),
        "year": ("year",),
    }

    def serves(self, grain: str, calendar: str) -> bool:
        if self.grain == "day":
            return True
        return self.calendar == calendar and grain in self.NESTS[self.grain]


class PreAggregation:
    """A summary table of ``measures`` by ``dimensions`` (and a time bucket).

    ``time_grain`` (``day`` by default; ``None`` for no time column) buckets the
    semantic model's ``time_dimension`` (or the one named). Day buckets answer
    every grain in both calendars; a coarser grain needs ``calendar`` (default
    ``GOZAR["DEFAULT_CALENDAR"]``) and answers only that calendar's same or
    coarser grains over whole buckets. ``scope_fields`` are extra lookup paths
    kept as columns so row-level rules reading them can be applied to the table;
    without them a user whose rules read other fields is answered by the ORM.
    ``refresh_every`` / ``rebuild_every`` / ``max_staleness`` (seconds or a
    ``timedelta``) override ``GOZAR["PREAGG_*"]``; ``incremental_window`` (days
    or a ``timedelta``) makes the scheduled refresh recompute only recent
    buckets plus the ones writes touched, instead of rebuilding."""

    def __init__(
        self,
        name: str,
        *,
        measures: list[str] | tuple[str, ...],
        dimensions: list[str] | tuple[str, ...] = (),
        time_dimension: str | None = None,
        time_grain: str | None = "day",
        calendar: str | None = None,
        scope_fields: list[str] | tuple[str, ...] = (),
        refresh_every: float | dt.timedelta | None = None,
        rebuild_every: float | dt.timedelta | None = None,
        incremental_window: int | dt.timedelta | None = None,
        max_staleness: float | dt.timedelta | None = None,
        label: TextInput = None,
        description: TextInput = None,
    ):
        self.name = name
        self.measure_keys = list(measures)
        self.dimension_keys = list(dimensions)
        self.time_dimension_key = time_dimension
        self.time_grain = time_grain
        self.calendar = calendar
        self.scope_paths = list(scope_fields)
        self.refresh_every = seconds(refresh_every)
        self.rebuild_every = seconds(rebuild_every)
        if isinstance(incremental_window, dt.timedelta):
            incremental_window = incremental_window.days
        self.incremental_window = incremental_window
        self.max_staleness = seconds(max_staleness)
        self._label = label
        self._description = description
        # filled by bind()
        self.semantic = None
        self.key = ""
        self.label: Text = {}
        self.description: Text = {}
        self.time_dim = None
        self.grain: Grain | None = None
        self.dims: dict[str, Any] = {}
        self.columns: list[Column] = []
        self.decompositions: dict[str, additive.Decomposition] = {}
        self.parts: list[additive.Part] = []
        self._definition: str | None = None

    def __repr__(self) -> str:
        return f"<PreAggregation {self.key or self.name}>"

    # -- binding ----------------------------------------------------------------------

    def bind(self, semantic) -> PreAggregation:
        """A bound copy for ``semantic``; raises ``ImproperlyConfigured`` for a
        declaration that can't be answered exactly."""
        bound = object.__new__(PreAggregation)
        bound.__dict__ = {**self.__dict__, "_definition": None}
        bound._bind(semantic)
        return bound

    def _fail(self, message: str):
        raise ImproperlyConfigured(
            f"{self.semantic.name}.pre_aggregations[{self.name!r}]: {message}"
        )

    def _bind(self, semantic) -> None:
        self.semantic = semantic
        if not isinstance(self.name, str) or not NAME_RE.match(self.name):
            self._fail("the name must be lower-case letters, digits and underscores")
        self.key = f"{semantic.name}.{self.name}"
        model = semantic.model
        self.label = text(self._label, fa=self.name, en=self.name)
        self.description = text(self._description)
        if not self.measure_keys:
            self._fail("list at least one measure")
        self.columns = []

        # time bucket (a model without a time dimension gets none by default)
        if self.time_dimension_key is None and semantic.time_dimension is None:
            self.time_grain = None
        if self.time_grain is not None:
            if self.time_grain not in TIME_GRAINS:
                self._fail(f"unknown time_grain {self.time_grain!r}")
            key = self.time_dimension_key or semantic.time_dimension
            dim = semantic.dimensions.get(key) if key else None
            if dim is None or dim.type != "time":
                self._fail(f"{key!r} is not a time dimension of {semantic.name}")
            self._check_paths(dim.expr, f"time dimension {key!r}")
            self.time_dim = dim
            calendar = self.calendar
            if self.time_grain == "day":
                calendar = None
            elif calendar is None:
                from bi_admin import preferences

                calendar = preferences.default_calendar()
            elif calendar not in CALENDARS:
                self._fail(f"unknown calendar {calendar!r}")
            self.grain = Grain(self.time_grain, calendar)
            self.columns.append(Column(TIME_COLUMN, "time", models.DateField(null=True), key))
        elif self.time_dimension_key is not None:
            self._fail("time_dimension needs a time_grain")

        # dimensions
        self.dims = {}
        for key in self.dimension_keys:
            dim = semantic.dimensions.get(key)
            if dim is None:
                self._fail(f"unknown dimension {key!r}")
            if dim.type == "time":
                self._fail(f"time dimension {key!r} can only be the time_dimension")
            self._check_paths(dim.expr, f"dimension {key!r}")
            if dim.path is not None:
                path = paths.walk(model, dim.path)
                output = path.field
                canonical = path.path
            else:
                output = additive.resolved_output(model, dim.expr)
                canonical = None
            try:
                storage = additive.storage_field(output)
            except additive.NotAdditive as exc:
                self._fail(f"dimension {key!r}: {exc}")
            self.dims[key] = dim
            self.columns.append(Column(f"d_{key}"[:60], "dimension", storage, key, canonical))

        # extra columns for row-level rules
        seen = {c.path for c in self.columns if c.path}
        for i, raw in enumerate(self.scope_paths):
            try:
                path = paths.walk(model, raw)
            except LookupError as exc:
                self._fail(f"scope field {raw!r}: {exc}")
            if path.many or path.reverse:
                self._fail(f"scope field {raw!r} crosses a to-many relation")
            if path.path in seen:
                continue
            seen.add(path.path)
            storage = additive.storage_field(path.field)
            self.columns.append(Column(f"s_{i}", "scope", storage, raw, path.path))

        # measures → parts
        self.decompositions = {}
        self.parts = []
        for key in self.measure_keys:
            measure = semantic.measures.get(key)
            if measure is None:
                self._fail(f"unknown measure {key!r}")
            self._check_paths(measure.expr, f"measure {key!r}")
            try:
                found = additive.decompose(model, key, measure.expr)
            except additive.NotAdditive as exc:
                self._fail(f"measure {key!r} is not additive: {exc}")
            for part in found.parts:
                same = next((p for p in self.parts if p.expr == part.expr), None)
                if same is None:
                    part.column = f"m_{len(self.parts)}"
                    self.parts.append(part)
                else:
                    part.column = same.column
            self.decompositions[key] = found
        for part in self.parts:
            self.columns.append(Column(part.column, "part", part.field))

    def _check_paths(self, expr, what: str) -> None:
        try:
            found = paths.expression_paths(self.semantic.model, expr)
        except LookupError as exc:
            self._fail(f"{what}: {exc}")
        for path in found:
            walked = paths.walk(self.semantic.model, path)
            if walked.many or walked.reverse:
                self._fail(
                    f"{what} reads {path!r} across a to-many relation; a summary table "
                    "would count its rows more than once"
                )

    # -- settings ---------------------------------------------------------------------

    def setting(self, name: str) -> float:
        from bi_admin.conf import setting

        own = {
            "REFRESH_EVERY": self.refresh_every,
            "REBUILD_EVERY": self.rebuild_every,
            "MAX_STALENESS": self.max_staleness,
        }[name]
        return float(own if own is not None else (setting(f"PREAGG_{name}") or 0))

    # -- definition and storage -------------------------------------------------------

    @property
    def definition(self) -> str:
        """Hash of everything the stored rows depend on; a change means a rebuild."""
        if self._definition is None:
            from bi_admin.access.rules import scope_key

            model = self.semantic.model
            payload = {
                "format": FORMAT_VERSION,
                "semantic": self.semantic.name,
                "model": self.semantic.model_label,
                "db_table": model._meta.db_table,
                "time_zone": settings.TIME_ZONE,
                "base": scope_key(self.semantic.get_queryset(None)),
                "grain": [self.grain.grain, self.grain.calendar] if self.grain else None,
                "time": repr(self.time_dim.expr) if self.time_dim else None,
                "columns": [
                    [c.name, c.role, c.key, c.path, repr(c.field.deconstruct()[1:])]
                    for c in self.columns
                ],
                "dimensions": {k: repr(d.expr) for k, d in self.dims.items()},
                "parts": [[p.column, repr(p.expr)] for p in self.parts],
            }
            raw = json.dumps(payload, sort_keys=True, default=str)
            self._definition = hashlib.sha256(raw.encode()).hexdigest()
        return self._definition

    def table_name(self, generation: int) -> str:
        slug = f"{self.semantic.name[:16]}_{self.name[:16]}"
        return f"{TABLE_PREFIX}{slug}_{self.definition[:8]}_{generation}"

    def model_for(self, table: str) -> type[models.Model]:
        """An unmanaged model over summary table ``table`` (in Gozar's own app registry)."""
        key = (table, self.definition)
        if key not in _MODELS:
            token = hashlib.sha1("/".join(key).encode()).hexdigest()[:10]
            indexes = []
            if self.grain is not None:
                indexes.append(models.Index(fields=[TIME_COLUMN], name=f"gpa_{token}_t"))
            meta = type(
                "Meta",
                (),
                {
                    "app_label": "gozar_scale",
                    "db_table": table,
                    "managed": False,
                    "apps": APPS,
                    "indexes": indexes,
                },
            )
            attrs: dict[str, Any] = {
                "__module__": __name__,
                "Meta": meta,
                "id": models.BigAutoField(primary_key=True),
            }
            for column in self.columns:
                attrs[column.name] = column.field.clone()
            _MODELS[key] = type(f"PreAggregation_{token}", (models.Model,), attrs)
        return _MODELS[key]

    # -- build query ------------------------------------------------------------------

    def build_queryset(self, source: models.QuerySet, window=None) -> models.QuerySet:
        """``source`` (the semantic model's rows) grouped into summary rows; with
        ``window = (start, end)`` only rows whose local date is in it (plus the null
        time bucket)."""
        from django.db.models import Q

        from bi_admin.engine.compiler import bucket_expr, date_range_q, local_date
        from bi_admin.engine.validator import GroupKey

        qs = source
        groups: dict[str, Any] = {}
        if self.time_dim is not None:
            dim = self.time_dim
            lookup = dim.path
            if lookup is None:
                qs = qs.alias(_pa_time=dim.expr)
                lookup = "_pa_time"
            if window is not None:
                start, end = window
                qs = qs.filter(
                    date_range_q(lookup, start, end, dim.is_date) | Q(**{f"{lookup}__isnull": True})
                )
            calendar = self.grain.calendar or "gregorian"
            if self.grain.grain != "day" and calendar == "jalali":
                qs = qs.alias(_pa_day=local_date(lookup, dim.is_date))
                lookup = "_pa_day"
            groups[TIME_COLUMN] = bucket_expr(GroupKey(dim, self.grain.grain), calendar, lookup)
        for column in self.columns:
            if column.role == "dimension":
                dim = self.dims[column.key]
                groups[column.name] = F(dim.path) if dim.path is not None else dim.expr
            elif column.role == "scope":
                groups[column.name] = F(column.path)
        parts = {p.column: p.expr for p in self.parts}
        return qs.annotate(**groups).values(*groups).annotate(**parts).order_by()

    def window_for(self, start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
        """``[start, end]`` widened to whole buckets of this pre-aggregation's grain."""
        from bi_admin.calendar.periods import shift, unit_start

        if self.grain is None or self.grain.grain == "day":
            return start, end
        calendar = self.grain.calendar
        first = unit_start(start, self.grain.grain, calendar)
        last = shift(unit_start(end, self.grain.grain, calendar), self.grain.grain, 1, calendar)
        return first, last - dt.timedelta(days=1)

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "semantic_model": self.semantic.name,
            "label": self.label,
            "description": self.description,
            "measures": list(self.measure_keys),
            "dimensions": list(self.dimension_keys),
            "time_dimension": self.time_dim.key if self.time_dim else None,
            "time_grain": self.grain.grain if self.grain else None,
            "calendar": self.grain.calendar if self.grain else None,
            "scope_fields": list(self.scope_paths),
            "incremental_window": self.incremental_window,
            "refresh_every": self.setting("REFRESH_EVERY"),
            "rebuild_every": self.setting("REBUILD_EVERY"),
            "max_staleness": self.setting("MAX_STALENESS"),
        }


def all_preaggregations() -> list[PreAggregation]:
    from bi_admin.semantic import semantic_site

    out = []
    for semantic in semantic_site.all():
        out.extend(semantic.pre_aggregations)
    return out


def get(key: str) -> PreAggregation:
    for preagg in all_preaggregations():
        if preagg.key == key:
            return preagg
    raise LookupError(f"Unknown pre-aggregation {key!r}")
