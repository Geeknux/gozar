"""Report result cache (Django's cache framework).

A result is stored under a key built from the normalised spec (with the
resolved relative period), the user's row-level scope (the WHERE clause of
their scoped base queryset — rules and custom ``get_queryset`` alike), the
language, the reports database and the *data version* of every model the
report's SQL reads. A data version is a random token per model, replaced when
a row is saved or deleted (``post_save``/``post_delete``/``m2m_changed``, again
on commit so a reader that cached pre-commit data is invalidated too). Bulk
writes (``QuerySet.update``, ``bulk_create``, raw SQL) send no signals: call
``bump(Model)`` after them — Gozar does for CRUD actions and ``seed_demo``.

``GOZAR["REPORT_CACHE"]`` picks the cache alias, ``GOZAR["REPORT_CACHE_TIMEOUT"]``
the lifetime in seconds (0 disables the cache).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from functools import lru_cache
from typing import Any

from django.apps import apps
from django.core.cache import caches
from django.db import connections, transaction
from django.db.models.signals import m2m_changed, post_delete, post_save

from bi_admin.conf import setting
from bi_admin.discovery import model_label

KEY_PREFIX = "gozar:report:"
VERSION_PREFIX = "gozar:dv:"
EPOCH_KEY = "gozar:dv:*"
# Gozar's own bookkeeping never feeds a report; don't churn versions for it.
UNTRACKED = {
    "bi_admin.reportlog",
    "bi_admin.reportjob",
    "bi_admin.changelog",
    "bi_admin.userpreferences",
    "sessions.session",
}
UNTRACKED_APPS = {"sessions", "admin", "contenttypes", "messages"}


def timeout() -> int:
    return int(setting("REPORT_CACHE_TIMEOUT") or 0)


def enabled() -> bool:
    return timeout() > 0


def backend():
    return caches[setting("REPORT_CACHE") or "default"]


# --- data versions -------------------------------------------------------------------


def _label(model_or_label) -> str:
    if isinstance(model_or_label, str):
        return model_or_label.lower()
    return model_label(model_or_label)


def bump(*targets) -> None:
    """Invalidate cached reports reading these models (classes or labels)."""
    labels = {_label(t) for t in targets} - UNTRACKED
    if labels:
        backend().set_many({VERSION_PREFIX + label: uuid.uuid4().hex for label in labels}, None)


def bump_all() -> None:
    """Invalidate every cached report (e.g. after a bulk import)."""
    backend().set(EPOCH_KEY, uuid.uuid4().hex, None)


def versions(labels: list[str]) -> dict[str, str]:
    """Current data version of each model (plus the global epoch under ``*``).

    A missing version gets a fresh random token rather than a fixed default, so
    an evicted version can never resurrect results cached under an older one."""
    cache = backend()
    keys = [VERSION_PREFIX + label for label in labels] + [EPOCH_KEY]
    found = cache.get_many(keys)
    out = {}
    for key in keys:
        token = found.get(key)
        if token is None:
            cache.add(key, uuid.uuid4().hex, None)
            token = cache.get(key)
        out[key.removeprefix(VERSION_PREFIX)] = token
    return out


@lru_cache(maxsize=8)
def _tables(alias: str) -> dict[str, str]:
    quote = connections[alias].ops.quote_name
    models = apps.get_models(include_auto_created=True)
    return {quote(m._meta.db_table): model_label(m) for m in models}


def dependencies(sql: str, alias: str, always: list[str]) -> list[str]:
    """Labels of the models whose tables appear in ``sql`` (joins and subqueries)."""
    found = {label for table, label in _tables(alias).items() if table in sql}
    return sorted(found | set(always))


def _on_change(sender, **kwargs) -> None:
    if kwargs.get("raw"):  # loaddata
        bump(sender)
        return
    bump(sender)
    transaction.on_commit(lambda: bump(sender), using=kwargs.get("using"))


def _on_m2m(sender, instance, action, model, **kwargs) -> None:
    if action.startswith("post_"):
        targets = (sender, type(instance), model)
        bump(*targets)
        transaction.on_commit(lambda: bump(*targets), using=kwargs.get("using"))


def connect_signals() -> None:
    """Track saves and deletes of every model but Gozar's bookkeeping and Django's
    plumbing. Receivers are connected per model so those keep Django's fast
    (signal-less) bulk delete; tracked models give it up for correct invalidation."""
    for model in apps.get_models(include_auto_created=True):
        label = model_label(model)
        if label in UNTRACKED or model._meta.app_label in UNTRACKED_APPS:
            continue
        post_save.connect(_on_change, sender=model, dispatch_uid=f"gozar_cache_save:{label}")
        post_delete.connect(_on_change, sender=model, dispatch_uid=f"gozar_cache_del:{label}")
    m2m_changed.connect(_on_m2m, dispatch_uid="gozar_cache_m2m")


# --- results -------------------------------------------------------------------------


def report_key(parts: dict[str, Any]) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return KEY_PREFIX + hashlib.sha256(raw.encode()).hexdigest()


def get(key: str) -> dict | None:
    return backend().get(key)


def put(key: str, value: dict) -> None:
    backend().set(key, value, timeout())
