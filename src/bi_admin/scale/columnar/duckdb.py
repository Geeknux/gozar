"""DuckDB columnar engine (the optional ``duckdb`` extra).

**Sync** copies, from the reports database into one DuckDB file
(``GOZAR["DUCKDB"]["PATH"]``), every table the semantic models using the engine
read — their own model and every model their dimensions and measures join to —
plus ``CalendarDay``. Each table keeps its concrete columns; a datetime column
also gets ``<column>__ld``, its local date computed *by the primary database* with
the very ``TruncDate`` the ORM engine groups by, so day boundaries are identical.
Text dimensions get rank tables: their distinct values in the primary database's
``ORDER BY`` order, so sorting follows its collation (DuckDB compares bytes,
PostgreSQL's ``en_US`` does not). Rows are streamed through a CSV file into
``COPY`` (DuckDB's fastest load path without extra libraries), into a new file
that atomically replaces the old one; the data versions of every model read are
captured before reading and stored in the file, so freshness works like a
pre-aggregation's.

**Queries** are generated from the validated ``ReportSpec`` only: identifiers come
from Django's model metadata (quoted), every value is a ``?`` parameter. They
follow the ORM compiler: the same group keys, local-date filters as half-open
ranges, time buckets (``date_trunc`` for Gregorian grains — ISO weeks, quarters as
seasons — and the synced calendar table for Jalali ones), ``NULLS LAST`` ordering
with the remaining group keys as tie-breakers, ``LIMIT limit + 1`` for
truncation, the same ``409 calendar_not_populated`` check. Measure values are
converted the way the primary database and Django's converters would return them
(``numbers``). Row rules are translated condition by condition; anything the
engine can't reproduce exactly raises ``Unsupported`` and the ORM answers.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path as FilePath
from typing import Any

from django.db import connections, models
from django.db.models import Aggregate, Avg, Count, F, Max, Min, Q, Sum, Value
from django.db.models.expressions import CombinedExpression, Star
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone
from django.utils.translation import gettext as _

from bi_admin.access.rules import scope_key
from bi_admin.conf import duckdb_setting
from bi_admin.discovery import model_label
from bi_admin.engine.errors import ReportError
from bi_admin.execution import cache
from bi_admin.execution.router import reports_database
from bi_admin.models import CalendarDay, Materialization
from bi_admin.scale import paths, scope
from bi_admin.scale import state as states
from bi_admin.scale.columnar import ColumnarBackend, Unsupported
from bi_admin.scale.columnar.numbers import as_orm, average

logger = logging.getLogger("bi_admin.scale")
META = "_gozar_meta"
LOCAL_DATE = "__ld"
RANK_PREFIX = "_gozar_rank__"
FORMAT_VERSION = 1
ASCII_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
GREGORIAN_TRUNC = {"week": "week", "month": "month", "season": "quarter", "year": "year"}
JALALI_COLUMN = {
    "week": "jalali_week_start",
    "month": "jalali_month_start",
    "season": "jalali_season_start",
    "year": "jalali_year_start",
}
INTEGER_TYPES = {
    "AutoField",
    "BigAutoField",
    "SmallAutoField",
    "IntegerField",
    "BigIntegerField",
    "SmallIntegerField",
    "PositiveIntegerField",
    "PositiveBigIntegerField",
    "PositiveSmallIntegerField",
}
TEXT_TYPES = {
    "CharField",
    "TextField",
    "SlugField",
    "EmailField",
    "URLField",
    "FileField",
    "FilePathField",
    "GenericIPAddressField",
    "IPAddressField",
}


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def duck_type(field: models.Field) -> str | None:
    """DuckDB column type for a Django field (``None``: not copied)."""
    if field.is_relation:
        return duck_type(field.target_field)
    internal = field.get_internal_type()
    if internal in INTEGER_TYPES:
        return "BIGINT"
    if internal == "DecimalField":
        if field.max_digits is None or field.max_digits > 38:
            return None
        return f"DECIMAL({field.max_digits}, {field.decimal_places})"
    if internal == "FloatField":
        return "DOUBLE"
    if internal == "BooleanField":
        return "BOOLEAN"
    if internal == "DateField":
        return "DATE"
    if internal == "DateTimeField":
        return "TIMESTAMP"
    if internal == "UUIDField":
        return "UUID"
    if internal in TEXT_TYPES:
        return "VARCHAR"
    return None


def csv_value(value: Any) -> str:
    if value is None:
        return "\\N"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dt.datetime):
        if timezone.is_aware(value):
            value = value.astimezone(dt.UTC).replace(tzinfo=None)
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, int | float):
        return repr(value)
    text = str(value)
    return '"' + text.replace('"', '""') + '"'


def plain_param(value: Any) -> Any:
    """Choice enums and other subclasses as the plain values DuckDB binds."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    return value


def utc_naive(value: Any) -> Any:
    """Parameters for TIMESTAMP columns (stored as UTC without a zone)."""
    if isinstance(value, dt.datetime) and timezone.is_aware(value):
        return value.astimezone(dt.UTC).replace(tzinfo=None)
    return value


# --- what to copy ---------------------------------------------------------------------


@dataclass
class TablePlan:
    model: type[models.Model]
    columns: list[tuple[str, models.Field]] = field(default_factory=list)  # (column, field)
    local_dates: list[tuple[str, models.Field]] = field(default_factory=list)
    ranks: set[str] = field(default_factory=set)  # text columns needing collation ranks

    @property
    def table(self) -> str:
        return self.model._meta.db_table


def plan_tables(semantics) -> dict[str, TablePlan]:
    """Tables (by model label) to copy for ``semantics``, with their columns."""
    found: dict[str, TablePlan] = {}

    def add(model) -> TablePlan:
        label = model_label(model)
        if label not in found:
            plan = TablePlan(model)
            for f in model._meta.concrete_fields:
                if duck_type(f) is None:
                    continue
                plan.columns.append((f.column, f))
                if f.get_internal_type() == "DateTimeField":
                    plan.local_dates.append((f.column, f))
            found[label] = plan
        return found[label]

    add(CalendarDay)
    for sem in semantics:
        add(sem.model)
        exprs = [d.expr for d in sem.dimensions.values()] + [m.expr for m in sem.measures.values()]
        for expr in exprs:
            try:
                walked = [paths.walk(sem.model, p) for p in paths.expression_paths(sem.model, expr)]
            except LookupError:
                continue
            for path in walked:
                if path.many or path.reverse:
                    continue
                for hop in path.hops:
                    add(hop.target)
        for dim in sem.dimensions.values():
            if dim.path is None or dim.type not in ("category", "geo"):
                continue
            try:
                path = paths.walk(sem.model, dim.path)
            except LookupError:
                continue
            if not (path.many or path.reverse) and duck_type(path.field) == "VARCHAR":
                add(path.model).ranks.add(path.column)
    return found


# --- SQL generation ---------------------------------------------------------------------


class Builder:
    """FROM/JOIN/WHERE for one report over the synced tables."""

    def __init__(self, semantic, catalog: dict[str, set[str]]):
        self.semantic = semantic
        self.model = semantic.model
        self.catalog = catalog  # table → columns in the file
        self.joins: dict[tuple, str] = {}
        self.join_sql: list[str] = []
        self.params_join: list[Any] = []
        self.calendars: dict[str, str] = {}
        self.base = "t0"
        self.require(self.model._meta.db_table, [])

    def require(self, table: str, columns: list[str]) -> None:
        if table not in self.catalog:
            raise Unsupported("not_synced", f"table {table} isn't in the DuckDB copy")
        missing = [c for c in columns if c not in self.catalog[table]]
        if missing:
            raise Unsupported("not_synced", f"{table}.{missing[0]} isn't in the DuckDB copy")

    def alias_for(self, hops: tuple[paths.Hop, ...]) -> str:
        alias = self.base
        for i in range(len(hops)):
            key = tuple(h.field.name for h in hops[: i + 1])
            if key not in self.joins:
                hop = hops[i]
                target = hop.target._meta
                self.require(target.db_table, [hop.field.target_field.column])
                name = f"t{len(self.joins) + 1}"
                kind = "LEFT JOIN" if hop.nullable else "JOIN"
                self.join_sql.append(
                    f"{kind} {quote(target.db_table)} {name} ON {name}."
                    f"{quote(hop.field.target_field.column)} = {alias}.{quote(hop.field.column)}"
                )
                self.joins[key] = name
            alias = self.joins[key]
        return alias

    def column(self, path: paths.Path, local_date: bool = False) -> str:
        if path.many or path.reverse:
            raise Unsupported("to_many", f"{path.path} crosses a to-many relation")
        alias = self.alias_for(path.hops)
        column = path.column + (LOCAL_DATE if local_date else "")
        self.require(path.model._meta.db_table, [column])
        return f"{alias}.{quote(column)}"

    def path(self, raw: str) -> paths.Path:
        try:
            return paths.walk(self.model, raw)
        except LookupError as exc:
            raise Unsupported("lookup", str(exc)) from None

    def dimension(self, dim) -> tuple[str, paths.Path]:
        if dim.path is None:
            raise Unsupported("expression_dimension", f"{dim.key} is an expression")
        path = self.path(dim.path)
        return self.column(path), path

    def local_date(self, dim) -> str:
        if dim.path is None:
            raise Unsupported("expression_dimension", f"{dim.key} is an expression")
        path = self.path(dim.path)
        return self.column(path, local_date=not dim.is_date)

    def calendar(self, day_sql: str) -> str:
        if day_sql not in self.calendars:
            name = f"c{len(self.calendars)}"
            table = CalendarDay._meta.db_table
            self.require(table, ["date"])
            self.join_sql.append(f"LEFT JOIN {quote(table)} {name} ON {name}.date = {day_sql}")
            self.calendars[day_sql] = name
        return self.calendars[day_sql]

    def bucket(self, group, calendar: str) -> str:
        day = self.local_date(group.dimension)
        if group.grain == "day":
            return day
        if calendar == "gregorian":
            return f"CAST(date_trunc('{GREGORIAN_TRUNC[group.grain]}', {day}) AS DATE)"
        return f"{self.calendar(day)}.{JALALI_COLUMN[group.grain]}"


def _negate(sql: str) -> str:
    # Django's exclude(): rows where the condition isn't true, nulls included
    return f"(({sql}) IS NOT TRUE)"


def _in(column: str, values: list, params: list) -> str:
    if not values:
        return "FALSE"
    params.extend(values)
    return f"{column} IN ({', '.join('?' for _ in values)})"


class Report:
    """One validated report compiled to DuckDB SQL."""

    def __init__(self, report, request, catalog, vendor: str):
        self.report = report
        self.vendor = vendor
        self.b = Builder(report.semantic, catalog)
        self.where: list[str] = []
        self.params: list[Any] = []
        self.select: list[str] = []
        self.group: list[str] = []
        self.group_sql: dict[str, str] = {}
        self.rank_sql: dict[str, str] = {}
        self.measures: list[MeasurePlan] = []
        self.scope(request)
        self.filters()
        self.groups()
        self.measure_plans()

    # -- scope and filters --------------------------------------------------------

    def scope(self, request) -> None:
        sem = self.report.semantic
        if scope_key(sem.get_queryset(request)) != scope_key(sem.model._base_manager.all()):
            raise Unsupported("custom_queryset", "get_queryset selects other rows than the table")
        q = scope.rule_q(sem, request) if request is not None else None
        if q is not None:
            self.where.append(self.q_sql(q))

    def q_sql(self, q: Q) -> str:
        parts = []
        for child in q.children:
            if isinstance(child, Q):
                parts.append(self.q_sql(child))
                continue
            key, value = child
            try:
                path, lookup = paths.split_lookup(self.b.model, key)
            except LookupError as exc:
                raise Unsupported("scope_lookup", str(exc)) from None
            try:
                value = scope._plain(value, path, evaluate=True)
            except scope.Untranslatable as exc:
                raise Unsupported(exc.code, exc.detail) from None
            parts.append(self.lookup_sql(self.b.column(path), lookup, value, path))
        joiner = " AND " if q.connector == Q.AND else " OR "
        sql = "(" + joiner.join(parts) + ")" if parts else "TRUE"
        return _negate(sql) if q.negated else sql

    def lookup_sql(self, column: str, lookup: str, value: Any, path: paths.Path) -> str:
        params = self.params
        if path.value_field.get_internal_type() == "DateTimeField":
            value = [utc_naive(v) for v in value] if isinstance(value, list) else utc_naive(value)
        if lookup == "exact":
            if value is None:
                return f"{column} IS NULL"
            params.append(value)
            return f"{column} = ?"
        if lookup == "in":
            return _in(column, list(value), params)
        if lookup in ("gt", "gte", "lt", "lte"):
            params.append(value)
            return f"{column} {dict(gt='>', gte='>=', lt='<', lte='<=')[lookup]} ?"
        if lookup == "range":
            params.extend(value)
            return f"({column} >= ? AND {column} <= ?)"
        if lookup == "isnull":
            return f"{column} IS {'' if value else 'NOT '}NULL"
        raise Unsupported("scope_lookup", f"__{lookup} in a row rule")

    def contains(self, column: str, needle: str) -> str:
        """Django's ``icontains``: SQLite's ``LIKE`` folds ASCII letters only,
        PostgreSQL compares ``UPPER()`` (Unicode case mapping)."""
        if self.vendor == "sqlite":
            folded = needle.translate(str.maketrans(ASCII_UPPER, ASCII_UPPER.lower()))
            self.params.append(folded)
            return f"contains(translate({column}, '{ASCII_UPPER}', '{ASCII_UPPER.lower()}'), ?)"
        self.params.append(needle)
        return f"contains(upper({column}), upper(?))"

    def filters(self) -> None:
        r = self.report
        for f in r.filters:
            self.where.append(self.filter_sql(f))
        if r.period is not None and r.time_dimension is not None:
            day = self.b.local_date(r.time_dimension)
            self.params.extend([r.period.start, r.period.end])
            self.where.append(f"({day} >= ? AND {day} <= ?)")

    def filter_sql(self, f) -> str:
        dim, op, value = f.dimension, f.op, f.value
        params = self.params
        if dim.type == "time":
            day = self.b.local_date(dim)
            if op == "is_null":
                return f"{day} IS {'' if value else 'NOT '}NULL"
            if op in ("eq", "neq"):
                params.append(value)
                sql = f"{day} = ?"
                return sql if op == "eq" else _negate(sql)
            if op == "gte":
                params.append(value)
                return f"{day} >= ?"
            if op == "lte":
                params.append(value)
                return f"{day} <= ?"
            if op == "between":
                params.extend(value)
                return f"({day} >= ? AND {day} <= ?)"
            sql = _in(day, list(value), params)
            return sql if op == "in" else _negate(sql)
        column, _path = self.b.dimension(dim)
        if op == "is_null":
            return f"{column} IS {'' if value else 'NOT '}NULL"
        if op in ("eq", "neq"):
            params.append(value)
            sql = f"{column} = ?"
            return sql if op == "eq" else _negate(sql)
        if op in ("in", "not_in"):
            sql = _in(column, list(value), params)
            return sql if op == "in" else _negate(sql)
        if op == "gte":
            params.append(value)
            return f"{column} >= ?"
        if op == "lte":
            params.append(value)
            return f"{column} <= ?"
        if op == "between":
            params.extend(value)
            return f"({column} >= ? AND {column} <= ?)"
        if op == "contains":
            return self.contains(column, value)
        raise Unsupported("operator", op)

    # -- groups and measures ------------------------------------------------------

    def groups(self) -> None:
        r = self.report
        for i, g in enumerate(r.groups):
            if g.grain:
                sql = self.b.bucket(g, r.calendar)
            else:
                sql, path = self.b.dimension(g.dimension)
                if path.column in self.ranks_for(path):
                    table = RANK_PREFIX + path.model._meta.db_table + "__" + path.column
                    name = f"r{i}"
                    self.b.require(table, ["value", "rank"])
                    self.b.join_sql.append(
                        f"LEFT JOIN {quote(table)} {name} ON {name}.value = {sql}"
                    )
                    self.rank_sql[g.key] = f"MIN({name}.rank)"
            self.group_sql[g.key] = sql
            self.group.append(sql)

    def ranks_for(self, path: paths.Path) -> set[str]:
        if duck_type(path.field) != "VARCHAR":
            return set()
        table = RANK_PREFIX + path.model._meta.db_table + "__" + path.column
        return {path.column} if table in self.b.catalog else set()

    def measure_plans(self) -> None:
        for m in self.report.measures:
            self.measures.append(MeasurePlan.of(m, self))

    # -- the statement ------------------------------------------------------------

    def order_by(self) -> tuple[list[str], list[Any]]:
        r = self.report
        sort = list(r.sort)
        if not sort:
            sort = [(g.key, "asc") for g in r.groups if g.grain]
            sort = sort or [(r.measures[0].key, "desc")]
        done = {key for key, _ in sort}
        sort += [(g.key, "asc") for g in r.groups if g.key not in done]
        measures = {p.key: p for p in self.measures}
        out, params = [], []
        for key, direction in sort:
            if key in measures:
                expr = measures[key].order_sql
                params.extend(measures[key].order_params)
            else:
                expr = self.rank_sql.get(key, self.group_sql[key])
            out.append(f"{expr} {'DESC' if direction == 'desc' else 'ASC'} NULLS LAST")
        return out, params

    def from_where(self) -> str:
        sql = f"FROM {quote(self.report.semantic.model._meta.db_table)} t0"
        if self.b.join_sql:
            sql += " " + " ".join(self.b.join_sql)
        if self.where:
            sql += f" WHERE {' AND '.join(self.where)}"
        return sql

    def sql(self) -> tuple[str, list[Any]]:
        """The statement and its parameters, in the order they appear."""
        r = self.report
        select = [f"{sql} AS g{i}" for i, sql in enumerate(self.group)]
        params: list[Any] = []
        for p in self.measures:
            select.extend(p.select)
            params.extend(p.select_params)
        sql = f"SELECT {', '.join(select)} {self.from_where()}"
        params.extend(self.params)
        if self.group:
            order, order_params = self.order_by()
            sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(self.group)))
            sql += " ORDER BY " + ", ".join(order) + f" LIMIT {int(r.limit) + 1}"
            params.extend(order_params)
        return sql, [plain_param(v) for v in params]

    def calendar_check_sql(self) -> tuple[str, list[Any]] | None:
        """Min and max local dates of the rows, for Jalali buckets coarser than a day."""
        r = self.report
        if r.calendar != "jalali":
            return None
        days = [self.b.local_date(g.dimension) for g in r.groups if g.grain and g.grain != "day"]
        if not days:
            return None
        selects = ", ".join(f"MIN({d}), MAX({d})" for d in days)
        return f"SELECT {selects} {self.from_where()}", [plain_param(v) for v in self.params]


@dataclass
class MeasurePlan:
    key: str
    select: list[str]
    order_sql: str
    select_params: list[Any]
    order_params: list[Any]
    finish: Any  # fn(values of the select columns) → the measure's value

    @staticmethod
    def of(measure, report: Report) -> MeasurePlan:
        model = report.report.semantic.model
        vendor = report.vendor
        expr = measure.expr
        default = None
        if isinstance(expr, Coalesce):
            sources = expr.get_source_expressions()
            if len(sources) == 2 and isinstance(sources[1], Value):
                expr, default = sources[0], sources[1].value
        if not isinstance(expr, Aggregate) or type(expr) not in (Sum, Count, Avg, Min, Max):
            raise Unsupported("measure", f"{measure.key} isn't a single aggregate")
        if getattr(expr, "default", None) is not None:
            default = expr.default
        value_sql = report_value_sql(expr.source_expressions[0], report)
        flt, fparams = "", []
        if expr.filter is not None:
            sub = Report.__new__(Report)
            sub.b, sub.params, sub.vendor = report.b, fparams, vendor
            flt = f" FILTER (WHERE {sub.q_sql(expr.filter)})"
        output = resolved(model, expr)
        distinct = "DISTINCT " if getattr(expr, "distinct", False) else ""
        i = len(report.measures)
        if type(expr) is Avg:
            if distinct:
                raise Unsupported("measure", "distinct averages")
            field = source_field(model, expr.source_expressions[0])
            scale = getattr(field, "decimal_places", None) or 0
            total, count = f"SUM({value_sql}){flt}", f"COUNT({value_sql}){flt}"

            def finish(values, output=output, scale=scale):
                return average(values[0], values[1], output, scale, vendor)

            return MeasurePlan(
                measure.key,
                [f"{total} AS m{i}s", f"{count} AS m{i}c"],
                f"(CAST({total} AS DOUBLE) / NULLIF({count}, 0))",
                fparams * 2,
                fparams * 2,
                with_default(finish, default),
            )
        if type(expr) in (Min, Max) and output.get_internal_type() not in (
            INTEGER_TYPES | {"DecimalField", "FloatField"}
        ):
            raise Unsupported("measure", f"{measure.key}: min/max of a non-number")
        sql = f"{type(expr).__name__.upper()}({distinct}{value_sql}){flt}"

        def finish(values, output=output):
            return as_orm(values[0], output, vendor)

        return MeasurePlan(
            measure.key, [f"{sql} AS m{i}"], sql, fparams, fparams, with_default(finish, default)
        )


def with_default(fn, default):
    if default is None:
        return fn

    def finish(values):
        value = fn(values)
        return default if value is None else value

    return finish


def resolved(model, expr):
    from bi_admin.scale.additive import resolved_output

    return resolved_output(model, expr)


def source_field(model, expr) -> models.Field:
    if isinstance(expr, F):
        return paths.walk(model, expr.name).value_field
    return resolved(model, expr)


def report_value_sql(expr, report: Report) -> str:
    """SQL for what an aggregate reads: a field path, ``*``, a constant, or ``+ - *``
    of those."""
    if isinstance(expr, Star):
        return "*"
    if isinstance(expr, F):
        return report.b.column(report.b.path(expr.name))
    if (
        isinstance(expr, Value)
        and isinstance(expr.value, int | decimal.Decimal)
        and not (isinstance(expr.value, bool))
    ):
        return str(int(expr.value)) if isinstance(expr.value, int) else format(expr.value, "f")
    if isinstance(expr, CombinedExpression) and expr.connector in ("+", "-", "*"):
        lhs, rhs = expr.get_source_expressions()
        return f"({report_value_sql(lhs, report)} {expr.connector} {report_value_sql(rhs, report)})"
    raise Unsupported("measure", f"can't compute {expr!r} in DuckDB exactly")


# --- the backend ----------------------------------------------------------------------


class _Local(threading.local):
    connection = None
    identity = None


class DuckDBBackend(ColumnarBackend):
    name = "duckdb"
    label = "DuckDB"

    def __init__(self):
        self._local = _Local()
        self._lock = threading.Lock()
        self._labels: tuple[tuple, set[str]] | None = None

    # -- availability and configuration -------------------------------------------

    def available(self) -> bool:
        try:
            import duckdb  # noqa: F401
        except ImportError:
            return False
        return True

    def path(self) -> FilePath | None:
        configured = duckdb_setting("PATH")
        return FilePath(configured) if configured else None

    def config(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if duckdb_setting("THREADS"):
            out["threads"] = int(duckdb_setting("THREADS"))
        if duckdb_setting("MEMORY_LIMIT"):
            out["memory_limit"] = str(duckdb_setting("MEMORY_LIMIT"))
        return out

    def synced_labels(self) -> set[str]:
        sems = self.semantic_models()
        key = tuple(id(s) for s in sems)
        if self._labels is None or self._labels[0] != key:
            self._labels = (key, set(plan_tables(sems)))
        return set(self._labels[1])

    # -- sync ---------------------------------------------------------------------

    def sync(self) -> dict[str, Any]:
        """Copy the tables into a new file and switch to it."""
        if not self.available():
            raise RuntimeError("DuckDB isn't installed (pip install 'gozar[duckdb]')")
        target = self.path()
        if target is None:
            raise RuntimeError('Set GOZAR["DUCKDB"]["PATH"] to use the DuckDB engine')
        import duckdb

        row = states.ensure_state(Materialization.Kind.COLUMNAR, self.name)
        started = time.monotonic()
        alias = reports_database()
        sems = self.semantic_models()
        plans = plan_tables(sems)
        labels = sorted(plans)
        versions = cache.versions(labels)  # before reading anything
        changes = row.changes
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.csv")
        counts: dict[str, int] = {}
        try:
            con = duckdb.connect(str(tmp), config=self.config())
            try:
                for plan in plans.values():
                    counts[plan.table] = self._copy(con, plan, alias, staging)
                    for column in sorted(plan.ranks):
                        self._ranks(con, plan, column, alias)
                con.execute(f"CREATE TABLE {META} (key VARCHAR, value VARCHAR)")
                meta = {
                    "format": FORMAT_VERSION,
                    "versions": versions,
                    "labels": labels,
                    "synced_at": timezone.now().isoformat(),
                    "tables": counts,
                    "vendor": connections[alias].vendor,
                    "database": alias,
                    "time_zone": timezone.get_current_timezone_name(),
                    "semantic_models": [s.name for s in sems],
                }
                con.executemany(
                    f"INSERT INTO {META} VALUES (?, ?)",
                    [(k, json.dumps(v, default=str)) for k, v in meta.items()],
                )
                con.execute("CHECKPOINT")
            finally:
                con.close()
            os.replace(tmp, target)
        finally:
            for leftover in (tmp, staging, FilePath(f"{tmp}.wal")):
                if leftover.exists():
                    leftover.unlink()
        duration = int((time.monotonic() - started) * 1000)
        now = timezone.now()
        Materialization.objects.filter(pk=row.pk).update(
            status=Materialization.Status.READY,
            table=target.name,
            row_count=sum(counts.values()),
            size_bytes=target.stat().st_size,
            built_at=now,
            refreshed_at=now,
            refresh_mode="sync",
            duration_ms=duration,
            source_versions=versions,
            sources=states.encode_labels(labels),
            last_error="",
            details={"tables": counts, "semantic_models": [s.name for s in sems],
                     "changes_at_refresh": changes, "database": alias},
        )  # fmt: skip
        Materialization.objects.filter(pk=row.pk, changes=changes).update(
            stale_since=None, dirty_from=None, dirty_to=None, needs_rebuild=False
        )
        return {"name": self.name, "mode": "sync", "tables": counts, "duration_ms": duration,
                "rows": sum(counts.values())}  # fmt: skip

    def _copy(self, con, plan: TablePlan, alias: str, staging: FilePath) -> int:
        columns = [(c, duck_type(f)) for c, f in plan.columns]
        columns += [(c + LOCAL_DATE, "DATE") for c, _f in plan.local_dates]
        con.execute(
            f"CREATE TABLE {quote(plan.table)} ("
            + ", ".join(f"{quote(c)} {t}" for c, t in columns)
            + ")"
        )
        tz = timezone.get_current_timezone()
        annotations = {
            f"_ld{i}": TruncDate(f.name, tzinfo=tz) for i, (_c, f) in enumerate(plan.local_dates)
        }
        names = [f.attname for _c, f in plan.columns] + list(annotations)
        qs = plan.model._base_manager.using(alias).order_by()
        if annotations:
            qs = qs.annotate(**annotations)
        count = 0
        with open(staging, "w", encoding="utf-8", newline="") as out:
            for values in qs.values_list(*names).iterator(chunk_size=20_000):
                out.write(",".join(csv_value(v) for v in values))
                out.write("\n")
                count += 1
        if count:
            literal = "'" + str(staging).replace("'", "''") + "'"
            con.execute(
                f"COPY {quote(plan.table)} FROM {literal} (FORMAT CSV, HEADER false, "
                "DELIMITER ',', QUOTE '\"', ESCAPE '\"', NULLSTR '\\N', "
                "ALLOW_QUOTED_NULLS false)"
            )
        return count

    def _ranks(self, con, plan: TablePlan, column: str, alias: str) -> None:
        field_name = next(f.name for c, f in plan.columns if c == column)
        values = (
            plan.model._base_manager.using(alias)
            .exclude(**{f"{field_name}__isnull": True})
            .order_by(field_name)
            .values_list(field_name, flat=True)
            .distinct()
        )
        table = quote(RANK_PREFIX + plan.table + "__" + column)
        con.execute(f"CREATE TABLE {table} (value VARCHAR, rank BIGINT)")
        batch: list[tuple[str, int]] = []
        for i, value in enumerate(values.iterator()):
            batch.append((value, i))
            if len(batch) >= 5000:
                con.executemany(f"INSERT INTO {table} VALUES (?, ?)", batch)
                batch = []
        if batch:
            con.executemany(f"INSERT INTO {table} VALUES (?, ?)", batch)

    # -- reading ------------------------------------------------------------------

    def connect(self):
        """A read-only connection to the current file (reopened after a sync)."""
        import duckdb

        target = self.path()
        if target is None or not target.exists():
            raise Unsupported("not_synced", "the DuckDB file doesn't exist yet")
        stat = target.stat()
        identity = (str(target), stat.st_ino, stat.st_mtime_ns)
        local = self._local
        if local.connection is None or local.identity != identity:
            if local.connection is not None:
                local.connection.close()
            local.connection = duckdb.connect(str(target), read_only=True, config=self.config())
            local.identity = identity
            rows = local.connection.execute(f"SELECT key, value FROM {META}").fetchall()
            local.meta = {k: json.loads(v) for k, v in rows}
            tables = local.connection.execute(
                "SELECT table_name, column_name FROM information_schema.columns"
            ).fetchall()
            catalog: dict[str, set[str]] = {}
            for table, column in tables:
                catalog.setdefault(table, set()).add(column)
            local.catalog = catalog
        return local.connection

    def freshness(self, meta: dict) -> states.Freshness:
        """The file's own captured versions against the current ones; the lag runs
        from the first change after the sync (``stale_since`` on the state row)."""
        if cache.versions(meta.get("labels") or []) == meta.get("versions"):
            return states.Freshness(True)
        row = states.get_state(Materialization.Kind.COLUMNAR, self.name)
        if row is None:
            return states.Freshness(False)
        now = timezone.now()
        since = row.stale_since
        if since is None:
            Materialization.objects.filter(pk=row.pk, stale_since__isnull=True).update(
                stale_since=now
            )
            since = now
        return states.Freshness(False, since, max(0.0, (now - since).total_seconds()))

    def execute(self, reports: list, request):
        if not self.available():
            raise Unsupported("not_installed", "the duckdb package isn't installed")
        con = self.connect()
        meta, catalog = self._local.meta, self._local.catalog
        if meta.get("format") != FORMAT_VERSION:
            raise Unsupported("not_synced", "the DuckDB file has an older format")
        vendor = connections[reports_database()].vendor
        if meta.get("vendor") != vendor or meta.get("time_zone") != (
            timezone.get_current_timezone_name()
        ):
            raise Unsupported("not_synced", "the DuckDB copy came from another database or zone")
        found = self.freshness(meta)
        limit = float(duckdb_setting("MAX_STALENESS") or 0)
        lag = found.lag_seconds
        if not found.fresh and not (lag is not None and 0 < limit and lag <= limit):
            raise Unsupported("stale", f"{lag or 0:.0f}s")
        compiled = [Report(r, request, catalog, vendor) for r in reports]
        results = [self._run(con, c) for c in compiled]
        return results, {"stale": not found.fresh, **found.as_dict()}

    def _run(self, con, compiled: Report) -> tuple[list[dict], bool]:
        from bi_admin.calendar import jalali
        from bi_admin.engine.runner import number
        from bi_admin.execution.limits import timeout_ms

        report = compiled.report
        ms = timeout_ms()
        timer = threading.Timer(ms / 1000, con.interrupt) if ms > 0 else None
        if timer is not None:
            timer.daemon = True
            timer.start()
        try:
            check = compiled.calendar_check_sql()
            if check is not None:
                self._check_calendar(con, *check)
            sql, params = compiled.sql()
            fetched = con.execute(sql, params).fetchall()
        except Exception as exc:
            import duckdb

            if isinstance(exc, duckdb.InterruptException):
                from bi_admin.execution.limits import timeout_error

                raise timeout_error(ms) from exc
            raise
        finally:
            if timer is not None:
                timer.cancel()
        groups = len(report.groups)
        rows = []
        for raw in fetched[: report.limit] if report.groups else fetched:
            row: dict[str, Any] = {}
            for i, g in enumerate(report.groups):
                value = raw[i]
                if g.grain:
                    if isinstance(value, dt.datetime):
                        value = value.date()
                    row[g.key] = value.isoformat() if value is not None else None
                    row[f"{g.key}__label"] = (
                        jalali.bucket_label(value, g.grain, report.calendar)
                        if value is not None
                        else None
                    )
                else:
                    row[g.key] = _cell(value)
            position = groups
            for plan in compiled.measures:
                width = len(plan.select)
                row[plan.key] = number(plan.finish(list(raw[position : position + width])))
                position += width
            rows.append(row)
        truncated = bool(report.groups) and len(fetched) > report.limit
        return rows, truncated

    def _check_calendar(self, con, sql: str, params: list) -> None:
        values = con.execute(sql, params).fetchone()
        table = quote(CalendarDay._meta.db_table)
        for lo, hi in zip(values[::2], values[1::2], strict=True):
            if lo is None:
                continue
            have = con.execute(
                f"SELECT COUNT(*) FROM {table} WHERE date BETWEEN ? AND ?", [lo, hi]
            ).fetchone()[0]
            missing = (hi - lo).days + 1 - have
            if missing:
                raise ReportError(
                    409,
                    "calendar_not_populated",
                    _(
                        "The calendar table lacks %(missing)s day(s) between %(start)s and "
                        "%(end)s; run `manage.py gozar_calendar`"
                    )
                    % {"missing": missing, "start": lo, "end": hi},
                    start=lo.isoformat(),
                    end=hi.isoformat(),
                )

    # -- System page ----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        target = self.path()
        row = states.get_state(Materialization.Kind.COLUMNAR, self.name)
        out: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "installed": self.available(),
            "configured": target is not None,
            "file": target.name if target is not None else None,
            "exists": bool(target is not None and target.exists()),
            "size_bytes": target.stat().st_size if target is not None and target.exists() else None,
            "semantic_models": [s.name for s in self.semantic_models()],
            "max_staleness": float(duckdb_setting("MAX_STALENESS") or 0),
            "sync_every": float(duckdb_setting("SYNC_EVERY") or 0),
            "state": None,
        }
        if row is not None:
            out["state"] = {
                "status": row.status,
                "synced_at": row.refreshed_at.isoformat() if row.refreshed_at else None,
                "duration_ms": row.duration_ms,
                "rows": row.row_count,
                "tables": (row.details or {}).get("tables", {}),
                "last_error": row.last_error,
                "hits": row.hits,
                "last_hit_at": row.last_hit_at.isoformat() if row.last_hit_at else None,
                "freshness": states.freshness(row).as_dict() if row.source_versions else None,
            }
        return out


def _cell(value: Any) -> Any:
    from bi_admin.engine.runner import cell

    if isinstance(value, dt.datetime):
        return value.isoformat()
    return cell(value)
