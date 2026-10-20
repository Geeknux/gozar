"""Splitting a measure into additive parts that a summary table can store.

A measure is an ORM expression whose leaves are aggregates. Each leaf becomes one
or two *parts* — aggregates computed per summary row — and a *re-aggregation*
that combines the parts of many summary rows into the leaf's value:

=====================  ==========================  ==============================
measure leaf           parts (per summary row)     re-aggregation
=====================  ==========================  ==============================
``Sum(x, filter=f)``   ``Sum(x, filter=f)``        ``Sum(part)``
``Count(x, filter)``   ``Count(x, filter)``        ``Coalesce(Sum(part), 0)``
``Avg(x, filter)``     ``Sum(x, f)``, ``Count(x,   ``Sum(s) / NULLIF(Sum(c), 0)``
                       f)``
``Min`` / ``Max``      ``Min(x)`` / ``Max(x)``     ``Min(part)`` / ``Max(part)``
=====================  ==========================  ==============================

Arithmetic around the leaves (``Sum(a) / Count(b)``, ``Coalesce(Sum(a), 0)``) is
kept as it is, with the leaves replaced. Distinct counts and any other aggregate
are not additive and raise ``NotAdditive``.

Exactness: every re-aggregation keeps the leaf's own output field (so Django's
converters treat the value identically), and the average's division is written so
the database computes what its own ``AVG`` does — on PostgreSQL ``numeric /
numeric`` (``AVG(numeric)`` is that same division), on SQLite a floating-point
division of the exact integer sum (what SQLite's ``avg()`` does).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import models
from django.db.models import Aggregate, Avg, Count, F, Func, Max, Min, Sum, Value
from django.db.models.functions import Coalesce

ADDITIVE = (Sum, Count, Avg, Min, Max)


class NotAdditive(Exception):
    """The measure can't be rebuilt from additive parts."""


class AvgOfParts(Func):
    """``sum / NULLIF(count, 0)`` the way the database's own ``AVG`` divides."""

    arity = 2
    template = "(%(expressions)s)"
    arg_joiner = " / NULLIF("

    def as_sql(self, compiler, connection, **extra):
        sum_sql, sum_params = compiler.compile(self.source_expressions[0])
        count_sql, count_params = compiler.compile(self.source_expressions[1])
        return f"({sum_sql} / NULLIF({count_sql}, 0))", [*sum_params, *count_params]

    def as_sqlite(self, compiler, connection, **extra):
        sum_sql, sum_params = compiler.compile(self.source_expressions[0])
        count_sql, count_params = compiler.compile(self.source_expressions[1])
        return (
            f"(CAST({sum_sql} AS REAL) / NULLIF({count_sql}, 0))",
            [*sum_params, *count_params],
        )


@dataclass
class Part:
    """One stored column: an aggregate over the base rows of a summary row."""

    expr: Aggregate
    field: models.Field  # the stored column's type
    column: str = ""  # assigned by the pre-aggregation


@dataclass
class Decomposition:
    measure: str
    parts: list[Part] = field(default_factory=list)
    # builds the re-aggregation once the parts have columns: fn(column_of) → expression
    rebuild: object = None

    def expression(self, column_of) -> models.Expression:
        return self.rebuild(column_of)


def resolved_output(model: type[models.Model], expr) -> models.Field:
    """The output field ``expr`` resolves to on ``model`` (no query is run)."""
    query = model._default_manager.annotate(_gozar_out=expr).query
    return query.annotations["_gozar_out"].output_field


def storage_field(output: models.Field, *, sum_part: bool = False) -> models.Field:
    """A nullable column type for values of ``output`` (a summary-table column)."""
    internal = output.get_internal_type()
    if output.is_relation:
        return storage_field(output.target_field)
    if internal in (
        "AutoField",
        "BigAutoField",
        "SmallAutoField",
        "IntegerField",
        "BigIntegerField",
        "SmallIntegerField",
        "PositiveIntegerField",
        "PositiveBigIntegerField",
        "PositiveSmallIntegerField",
    ):
        return models.BigIntegerField(null=True)
    if internal == "DecimalField":
        places = output.decimal_places
        digits = output.max_digits
        if places is None or digits is None:
            return models.DecimalField(max_digits=38, decimal_places=10, null=True)
        # sums grow; 38 digits is the most every supported database stores exactly
        return models.DecimalField(
            max_digits=38 if sum_part else digits, decimal_places=places, null=True
        )
    if internal == "FloatField":
        return models.FloatField(null=True)
    if internal == "BooleanField":
        return models.BooleanField(null=True)
    if internal == "DateField":
        return models.DateField(null=True)
    if internal == "DateTimeField":
        return models.DateTimeField(null=True)
    if internal == "TimeField":
        return models.TimeField(null=True)
    if internal == "DurationField":
        return models.DurationField(null=True)
    if internal == "UUIDField":
        return models.UUIDField(null=True)
    if internal in (
        "CharField",
        "TextField",
        "SlugField",
        "EmailField",
        "URLField",
        "FileField",
        "FilePathField",
        "GenericIPAddressField",
        "IPAddressField",
    ):
        return models.TextField(null=True)
    raise NotAdditive(f"can't store a {internal} value in a summary table")


def _leaf_parts(model, agg: Aggregate) -> tuple[list[Part], object]:
    kind = type(agg)
    if kind not in ADDITIVE:
        raise NotAdditive(f"{kind.__name__} is not an additive aggregate")
    if getattr(agg, "distinct", False):
        raise NotAdditive("distinct counts can't be added up across summary rows")
    sources = list(agg.source_expressions)
    flt = agg.filter
    default = getattr(agg, "default", None)
    output = resolved_output(model, kind(*sources, filter=flt))

    def finish(expr):
        return Coalesce(expr, Value(default), output_field=output) if default is not None else expr

    if kind is Sum:
        if output.get_internal_type() == "FloatField":
            raise NotAdditive("floating-point sums can't be added up exactly")
        part = Part(Sum(*sources, filter=flt), storage_field(output, sum_part=True))
        return [part], lambda col: finish(Sum(F(col(part)), output_field=output))
    if kind is Count:
        part = Part(Count(*sources, filter=flt), models.BigIntegerField(null=True))
        return [part], lambda col: finish(
            Coalesce(Sum(F(col(part))), Value(0), output_field=output)
        )
    if kind in (Min, Max):
        part = Part(kind(*sources, filter=flt), storage_field(output))
        return [part], lambda col: finish(kind(F(col(part)), output_field=output))
    # Avg
    total = Sum(*sources, filter=flt)
    total_output = resolved_output(model, total)
    if total_output.get_internal_type() == "FloatField":
        raise NotAdditive("averages of floating-point values can't be rebuilt exactly")
    s = Part(total, storage_field(total_output, sum_part=True))
    c = Part(Count(*sources, filter=flt), models.BigIntegerField(null=True))
    return [s, c], lambda col: finish(
        AvgOfParts(Sum(F(col(s))), Sum(F(col(c))), output_field=output)
    )


def decompose(model: type[models.Model], key: str, expr) -> Decomposition:
    """Split measure ``key`` (expression ``expr`` on ``model``) into parts."""
    out = Decomposition(key)

    def walk(node):
        if isinstance(node, Aggregate):
            parts, rebuild = _leaf_parts(model, node)
            out.parts.extend(parts)
            return rebuild
        if not getattr(node, "contains_aggregate", False):
            return lambda col, node=node: node
        sources = node.get_source_expressions()
        builders = [walk(s) if s is not None else (lambda col: None) for s in sources]

        def rebuild(col, node=node, builders=builders):
            clone = node.copy()
            clone.set_source_expressions([b(col) for b in builders])
            return clone

        return rebuild

    out.rebuild = walk(expr)
    return out
