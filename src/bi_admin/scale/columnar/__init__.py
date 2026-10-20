"""Columnar report engines: an optional copy of the data in an analytical database.

A semantic model picks its engine with ``engine = "duckdb"`` (or
``GOZAR["SEMANTIC_ENGINES"] = {"order_metrics": "duckdb"}``, which wins); the
default is the ORM. Backends are listed in ``GOZAR["COLUMNAR_BACKENDS"]`` (name →
dotted path) and implement ``ColumnarBackend``:

* ``sync()`` copies the tables the semantic models using the backend read from
  the primary database, capturing their data versions first (like a
  pre-aggregation build), so freshness works the same way;
* ``execute(reports, request)`` runs a validated report (and its comparison) and
  returns rows shaped exactly as the ORM engine's, or raises ``Unsupported`` —
  then the ORM answers. It must never answer a scoped user with rows outside
  their scope.

DuckDB ships (``bi_admin.scale.columnar.duckdb``, the ``duckdb`` extra). A
ClickHouse backend would implement the same three methods plus ``status()``;
nothing else in Gozar needs to change.
"""

from __future__ import annotations

from typing import Any

from django.utils.module_loading import import_string

from bi_admin.conf import setting


class Unsupported(Exception):
    """The backend can't answer this report exactly; the ORM will."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class ColumnarBackend:
    name = ""
    label = ""

    def available(self) -> bool:
        """Whether the backend's library is installed."""
        raise NotImplementedError

    def semantic_models(self) -> list:
        """The semantic models whose engine is this backend."""
        from bi_admin.semantic import semantic_site

        return [s for s in semantic_site.all() if engine_name(s) == self.name]

    def synced_labels(self) -> set[str]:
        """Labels of the models a sync copies (no database access)."""
        raise NotImplementedError

    def sync(self) -> dict[str, Any]:
        raise NotImplementedError

    def execute(self, reports: list, request) -> tuple[list[tuple[list[dict], bool]], dict]:
        """Rows (and truncation) per report, plus execution details
        (``stale``, ``stale_since``, ``lag_seconds``)."""
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        raise NotImplementedError

    def sync_every(self) -> float:
        """Seconds between syncs of a stale copy by ``gozar_worker`` (0: never)."""
        return 0.0


_instances: dict[str, ColumnarBackend] = {}


def engine_name(semantic) -> str:
    configured = (setting("SEMANTIC_ENGINES") or {}).get(semantic.name)
    return configured or getattr(semantic, "engine", None) or "orm"


def names() -> list[str]:
    return list((setting("COLUMNAR_BACKENDS") or {}).keys())


def get(name: str) -> ColumnarBackend | None:
    path = (setting("COLUMNAR_BACKENDS") or {}).get(name)
    if path is None:
        return None
    key = f"{name}:{path}"
    if key not in _instances:
        _instances[key] = import_string(path)()
    return _instances[key]


def backend_for(semantic) -> ColumnarBackend | None:
    name = engine_name(semantic)
    return None if name == "orm" else get(name)


def in_use() -> list[ColumnarBackend]:
    """Configured backends at least one semantic model uses."""
    from bi_admin.semantic import semantic_site

    used = {engine_name(s) for s in semantic_site.all()}
    return [b for b in (get(n) for n in names() if n in used) if b is not None]


def synced_labels() -> dict[str, set[str]]:
    """What each backend in use copies, for change records (cheap, no queries)."""
    out = {}
    for backend in in_use():
        try:
            out[backend.name] = backend.synced_labels()
        except Exception:  # an unbindable model shows up in the system checks
            continue
    return out


def sync_due(now=None) -> list[dict[str, Any]]:
    """Sync every backend in use whose copy is missing, or stale for longer than its
    ``sync_every`` since the last sync (``gozar_worker``)."""
    import logging

    from django.utils import timezone

    from bi_admin.models import Materialization
    from bi_admin.scale import state as states

    now = now or timezone.now()
    out = []
    for backend in in_use():
        if not backend.available():
            continue
        row = states.get_state(Materialization.Kind.COLUMNAR, backend.name)
        every = backend.sync_every()
        if row is not None and row.status == Materialization.Status.READY:
            if not every or states.freshness(row, now).fresh:
                continue
            if row.refreshed_at and (now - row.refreshed_at).total_seconds() < every:
                continue
        try:
            out.append(backend.sync())
        except Exception as exc:
            logging.getLogger("bi_admin.scale").exception("Syncing %s failed", backend.name)
            out.append({"name": backend.name, "mode": "sync", "error": str(exc)})
    return out
