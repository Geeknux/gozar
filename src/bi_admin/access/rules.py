"""Row-level rules: which rows of a model (or semantic model) a user may see.

A rule is a callable taking the user and returning

* a ``Q`` — the user sees only rows matching it;
* ``True`` or ``None`` — no restriction from this rule;
* ``False`` — no rows at all.

Rules are declared in code, next to the models they protect::

    from bi_admin.access import rule

    rule(Order, lambda user: Q(customer__city__in=user.profile.cities.all()))
    rule(OrderMetrics, lambda user: Q(status="paid") if user.groups.filter(name="Sales")
         .exists() else True)

or as ``row_rules`` on ``ModelOptions`` / ``Semantic``. Every rule that applies
is AND-ed: rules on a model apply to its CRUD pages, to relation pickers that
list it and to every semantic model built on it; rules on a semantic model add
to those for reports only. Superusers bypass rules unless
``GOZAR["ROW_RULES_BYPASS_SUPERUSER"]`` is ``False``.

Rules take the user, not the request, so background jobs evaluate them exactly
as a request would. A rule that raises denies every row (fail closed) and the
error is logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from django.apps import apps
from django.core.exceptions import EmptyResultSet, FullResultSet
from django.db import models
from django.db.models import Q

from bi_admin.conf import setting
from bi_admin.discovery import get_registry, model_label

logger = logging.getLogger("bi_admin.access")

Predicate = Callable[[Any], Q | bool | None]
NOTHING = Q(pk__in=[])


class RuleRegistry:
    def __init__(self):
        self.models: dict[str, list[Predicate]] = {}
        self.semantics: dict[str, list[Predicate]] = {}

    def add(self, target, predicate: Predicate) -> None:
        if not callable(predicate):
            raise TypeError(f"A row rule must be callable, not {predicate!r}")
        kind, key = _target_key(target)
        bucket = self.models if kind == "model" else self.semantics
        bucket.setdefault(key, []).append(predicate)

    def remove(self, target, predicate: Predicate | None = None) -> None:
        kind, key = _target_key(target)
        bucket = self.models if kind == "model" else self.semantics
        if predicate is None:
            bucket.pop(key, None)
        elif predicate in bucket.get(key, []):
            bucket[key].remove(predicate)

    def for_model(self, label: str) -> list[Predicate]:
        return list(self.models.get(label, []))

    def for_semantic(self, name: str) -> list[Predicate]:
        return list(self.semantics.get(name, []))


def _target_key(target) -> tuple[str, str]:
    from bi_admin.semantic.base import Semantic, _snake

    if isinstance(target, type) and issubclass(target, Semantic):
        return "semantic", target.name or _snake(target.__name__)
    if isinstance(target, Semantic):
        return "semantic", target.name
    if isinstance(target, type) and issubclass(target, models.Model):
        return "model", model_label(target)
    if isinstance(target, str) and "." in target:
        return "model", target.lower()
    raise TypeError(f"rule() targets a model, an 'app.model' label or a Semantic, not {target!r}")


registry = RuleRegistry()


def rule(target, predicate: Predicate | None = None):
    """Register a row rule; ``@rule(Order)`` also works as a decorator."""
    if predicate is None:

        def wrap(fn: Predicate) -> Predicate:
            registry.add(target, fn)
            return fn

        return wrap
    registry.add(target, predicate)
    return predicate


# --- evaluation --------------------------------------------------------------------


def bypasses(user) -> bool:
    return bool(
        user is not None
        and getattr(user, "is_superuser", False)
        and setting("ROW_RULES_BYPASS_SUPERUSER")
    )


def _evaluate(predicates: list[Predicate], user, where: str) -> Q | None:
    combined: Q | None = None
    for predicate in predicates:
        try:
            result = predicate(user)
        except Exception:
            logger.exception("Row rule %r for %s failed; denying access", predicate, where)
            return NOTHING
        if result is None or result is True:
            continue
        if result is False:
            return NOTHING
        if not isinstance(result, Q):
            logger.error("Row rule %r for %s returned %r; denying access", predicate, where, result)
            return NOTHING
        combined = result if combined is None else combined & result
    return combined


def _user(request):
    return getattr(request, "user", None) if request is not None else None


def _memo(request, key: tuple, compute: Callable[[], Q | None]) -> Q | None:
    """Rules may query (``user.groups``…); evaluate each once per request."""
    cache = getattr(request, "_gozar_rules", None)
    if cache is None:
        cache = {}
        try:
            request._gozar_rules = cache
        except AttributeError:
            return compute()
    if key not in cache:
        cache[key] = compute()
    return cache[key]


def model_predicates(label: str) -> list[Predicate]:
    predicates = registry.for_model(label)
    if label in get_registry():
        from bi_admin.crud import site

        predicates += list(site.options_for(label).row_rules)
    return predicates


def model_filter(model, request) -> Q | None:
    """The combined rule ``Q`` for ``model`` (``None``: unrestricted)."""
    label = model if isinstance(model, str) else model_label(model)
    user = _user(request)
    if request is None or bypasses(user):
        return None
    return _memo(request, ("model", label), lambda: _evaluate(model_predicates(label), user, label))


def semantic_filter(semantic, request) -> Q | None:
    """Model rules of the semantic model's model AND its own rules."""
    user = _user(request)
    if request is None or bypasses(user):
        return None

    def compute():
        base = _evaluate(model_predicates(semantic.model_label), user, semantic.model_label)
        own = _evaluate(
            [*registry.for_semantic(semantic.name), *semantic.row_rules], user, semantic.name
        )
        if base is None or own is None:
            return own if base is None else base
        return base & own

    return _memo(request, ("semantic", semantic.name), compute)


def restrict(qs: models.QuerySet, q: Q | None) -> models.QuerySet:
    return qs if q is None else qs.filter(q)


def model_queryset(options, request) -> models.QuerySet:
    """``ModelOptions.get_queryset`` narrowed by the row rules (CRUD and pickers)."""
    return restrict(options.get_queryset(request), model_filter(options.model, request))


def semantic_queryset(semantic, request) -> models.QuerySet:
    """``Semantic.get_queryset`` narrowed by the row rules (every report path)."""
    return restrict(semantic.get_queryset(request), semantic_filter(semantic, request))


def target_queryset(label: str, request) -> models.QuerySet:
    """Rows of any model a user may pick in a relation field (scoped when registered)."""
    if label in get_registry():
        from bi_admin.crud import site

        return model_queryset(site.options_for(label), request)
    model = apps.get_model(label)
    return restrict(model._default_manager.all(), model_filter(label, request))


def scope_text(qs: models.QuerySet) -> str:
    """The WHERE clause of ``qs`` as readable SQL: the user's effective scope, shown
    in the audit log. ``""`` means every row."""
    try:
        sql = str(qs.order_by().query)
    except EmptyResultSet:
        return "FALSE"
    except FullResultSet:
        return ""
    return sql.partition(" WHERE ")[2]


def scope_key(qs: models.QuerySet) -> str:
    """An exact fingerprint of the scope for the result-cache key: the SQL with its
    parameters kept apart and typed (``str(query)`` inlines them unquoted, so
    ``["a, b"]`` and ``["a", "b"]`` would read the same)."""
    try:
        sql, params = qs.order_by().query.sql_with_params()
    except EmptyResultSet:
        return "FALSE"
    except FullResultSet:
        return ""
    return repr((sql, tuple((type(p).__name__, repr(p)) for p in params)))
