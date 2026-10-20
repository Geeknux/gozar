"""A user's row-level scope, carried over to a pre-aggregation or a columnar copy.

Reports apply ``Semantic.get_queryset(request)`` plus the row rules
(``bi_admin.access.rules``). Another engine may answer for a user only when it can
apply exactly the same restriction:

* the semantic model's ``get_queryset`` must select the same rows for this user as
  for trusted code (the copy was built from the latter); and
* every condition of the combined rule ``Q`` must read a path the copy has — for a
  pre-aggregation, one of its dimension or ``scope_fields`` columns — with a plain
  value (model instances become primary keys; querysets become subqueries or, for
  a columnar copy, their evaluated values). Expressions (``F()``, ``OuterRef``) and
  paths the copy lacks make the scope untranslatable, and the ORM answers instead.

Untranslatable means "refuse", never "ignore": unscoped totals are never served to
a scoped user.
"""

from __future__ import annotations

from typing import Any

from django.db import models
from django.db.models import Q
from django.db.models.expressions import BaseExpression

from bi_admin.access.rules import NOTHING, scope_key, semantic_filter
from bi_admin.scale import paths


class Untranslatable(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def same_base(semantic, request) -> bool:
    """Whether ``get_queryset(request)`` selects what ``get_queryset(None)`` does."""
    if request is None:
        return True
    return scope_key(semantic.get_queryset(request)) == scope_key(semantic.get_queryset(None))


def rule_q(semantic, request) -> Q | None:
    """The user's combined rule ``Q`` (``None``: unrestricted)."""
    return semantic_filter(semantic, request)


def is_nothing(q: Q) -> bool:
    return q == NOTHING


def _first(row):
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0] if isinstance(row, tuple) else row


def _plain(value: Any, path: paths.Path, evaluate: bool) -> Any:
    if isinstance(value, models.Model):
        target = path.field.related_model if path.field.is_relation else None
        if target is None or not isinstance(value, target):
            raise Untranslatable("scope_value", f"{value!r} compared with {path.path}")
        return value.pk
    if isinstance(value, models.QuerySet):
        target = path.field.related_model if path.field.is_relation else None
        selected = list(value.query.values_select) + list(value.query.annotation_select)
        if not selected:  # a queryset of model instances: their primary keys
            if target is None or value.model is not target:
                raise Untranslatable("scope_value", f"a {value.model.__name__} queryset")
            value = value.values("pk")
        elif len(selected) != 1:
            raise Untranslatable("scope_value", "a queryset selecting several columns")
        if evaluate:
            return [_first(row) for row in value]
        return value
    if isinstance(value, BaseExpression) or hasattr(value, "resolve_expression"):
        raise Untranslatable("scope_expression", f"{path.path} is compared with an expression")
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(v, path, evaluate) for v in value]
    return value


def leaves(model: type[models.Model], q: Q, evaluate: bool = False):
    """Yield ``(path, lookup, value)`` for every condition of ``q`` (validated)."""
    for child in q.children:
        if isinstance(child, Q):
            yield from leaves(model, child, evaluate)
            continue
        key, value = child
        try:
            path, lookup = paths.split_lookup(model, key)
        except LookupError as exc:
            raise Untranslatable("scope_lookup", str(exc)) from None
        if path.many or path.reverse:
            raise Untranslatable("scope_to_many", f"{key} crosses a to-many relation")
        yield path, lookup, _plain(value, path, evaluate)


def to_columns(model: type[models.Model], q: Q, columns: dict[str, str]) -> Q:
    """``q`` over ``model`` rewritten for a summary table whose ``columns`` map
    canonical paths to column names; raises ``Untranslatable``."""
    out = Q()
    out.connector, out.negated = q.connector, q.negated
    for child in q.children:
        if isinstance(child, Q):
            out.children.append(to_columns(model, child, columns))
            continue
        key, value = child
        try:
            path, lookup = paths.split_lookup(model, key)
        except LookupError as exc:
            raise Untranslatable("scope_lookup", str(exc)) from None
        if lookup == "in" and isinstance(value, list | tuple | set) and not value:
            out.children.append(("pk__in", []))  # "no rows" needs no column
            continue
        if path.many or path.reverse:
            raise Untranslatable("scope_to_many", f"{key} crosses a to-many relation")
        column = columns.get(path.path)
        if column is None:
            raise Untranslatable("scope_not_covered", f"the row rules read {path.path}")
        out.children.append((f"{column}__{lookup}", _plain(value, path, evaluate=False)))
    return out
