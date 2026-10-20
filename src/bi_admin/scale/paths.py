"""ORM lookup paths walked with ``_meta``: the joins a path takes, whether it can
multiply rows and which column holds its value. Shared by pre-aggregations (which
store a path's value in a column) and the columnar engines (which join synced
tables themselves)."""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import F, Q
from django.db.models.expressions import BaseExpression

# Lookups a row-rule or filter condition may use on a translated path.
LOOKUPS = (
    "exact",
    "iexact",
    "in",
    "gt",
    "gte",
    "lt",
    "lte",
    "range",
    "isnull",
    "contains",
    "icontains",
    "startswith",
    "istartswith",
    "endswith",
    "iendswith",
)


@dataclass(frozen=True)
class Hop:
    """A forward foreign key (or one-to-one) from ``model`` to ``target``."""

    field: models.Field
    model: type[models.Model]
    target: type[models.Model]

    @property
    def nullable(self) -> bool:
        return bool(self.field.null)


@dataclass(frozen=True)
class Path:
    path: str  # canonical: a relation's own name, never "<relation>__pk"
    hops: tuple[Hop, ...]
    field: models.Field  # the field holding the value (a foreign key for a relation)
    model: type[models.Model]  # the model whose table has ``field``'s column
    many: bool = False  # crosses a to-many relation (rows may multiply)
    reverse: bool = False  # crosses a reverse one-to-one (no forward join)

    @property
    def nullable(self) -> bool:
        return bool(self.field.null) or any(h.nullable for h in self.hops)

    @property
    def column(self) -> str:
        return self.field.column

    @property
    def value_field(self) -> models.Field:
        """The field whose type the value has: a relation's target primary key."""
        field = self.field
        while field.is_relation:
            field = field.target_field
        return field


def walk(model: type[models.Model], path: str) -> Path:
    """Resolve ``path`` (``customer__city__name``) from ``model``; ``LookupError``
    for unknown parts and for transforms (``created_at__year``)."""
    parts = path.split("__") if path else []
    if not parts:
        raise LookupError("Empty path")
    current = model
    hops: list[Hop] = []
    names: list[str] = []
    many = reverse = False
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        opts = current._meta
        if part == "pk":
            part = opts.pk.name
        try:
            field = opts.get_field(part)
        except FieldDoesNotExist:
            raise LookupError(f"Unknown field {part!r} on {opts.label} (in {path!r})") from None
        if field.is_relation and field.concrete and (field.many_to_one or field.one_to_one):
            if last:
                names.append(field.name)
                return Path("__".join(names), tuple(hops), field, current, many, reverse)
            nxt = parts[i + 1]
            target = field.related_model
            if i + 1 == len(parts) - 1 and nxt in ("pk", target._meta.pk.name):
                # "customer__pk" / "customer__id" is the foreign key's own column
                names.append(field.name)
                return Path("__".join(names), tuple(hops), field, current, many, reverse)
            hops.append(Hop(field, current, target))
            names.append(field.name)
            current = target
            continue
        if field.is_relation:  # reverse relations and many-to-many
            if field.many_to_many or field.one_to_many:
                many = True
            else:
                reverse = True
            names.append(part)
            current = field.related_model
            if last:
                pk = current._meta.pk
                return Path("__".join(names), tuple(hops), pk, current, many, reverse)
            continue
        if not last:
            raise LookupError(f"{opts.label}.{part} is not a relation (in {path!r})")
        names.append(field.name)
        return Path("__".join(names), tuple(hops), field, current, many, reverse)
    raise LookupError(f"Empty path {path!r}")


def split_lookup(model: type[models.Model], key: str) -> tuple[Path, str]:
    """``customer__city__name_en__in`` → (the path to ``name_en``, ``"in"``)."""
    parts = key.split("__")
    for cut in range(len(parts), 0, -1):
        rest = parts[cut:]
        if len(rest) > 1 or (rest and rest[0] not in LOOKUPS):
            continue
        try:
            found = walk(model, "__".join(parts[:cut]))
        except LookupError:
            continue
        return found, rest[0] if rest else "exact"
    raise LookupError(f"Unsupported lookup {key!r}")


def q_paths(model: type[models.Model], q: Q) -> set[str]:
    """Canonical paths a ``Q`` reads (``LookupError`` for what can't be resolved)."""
    out: set[str] = set()
    for child in q.children:
        if isinstance(child, Q):
            out |= q_paths(model, child)
            continue
        key, value = child
        out.add(split_lookup(model, key)[0].path)
        if isinstance(value, BaseExpression | F):
            out |= expression_paths(model, value)
    return out


def expression_paths(model: type[models.Model], expr) -> set[str]:
    """Canonical paths an expression reads, its aggregates' ``filter=`` included."""
    if isinstance(expr, str):
        return {walk(model, expr).path}
    if isinstance(expr, F):
        return {walk(model, expr.name).path}
    if isinstance(expr, Q):
        return q_paths(model, expr)
    out: set[str] = set()
    if hasattr(expr, "get_source_expressions"):
        for source in expr.get_source_expressions():
            if source is not None:
                out |= expression_paths(model, source)
    for attr in ("filter", "condition"):
        extra = getattr(expr, attr, None)
        if isinstance(extra, Q | BaseExpression):
            out |= expression_paths(model, extra)
    return out
