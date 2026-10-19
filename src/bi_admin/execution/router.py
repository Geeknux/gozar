"""Read-replica routing for report queries.

Report, drill-down, export and value-lookup queries run inside ``reporting()``,
which points every read at ``GOZAR["REPORTS_DATABASE"]``; everything else —
CRUD reads and all writes, the audit log and jobs included — stays on
``default``. Add the router so that reads Gozar doesn't issue itself (lazy
related objects in a drill-down, a custom ``get_queryset``) follow too::

    DATABASE_ROUTERS = ["bi_admin.execution.router.ReportsRouter"]
    GOZAR = {"REPORTS_DATABASE": "replica"}

The engine also passes the alias explicitly (``QuerySet.using``), so the main
report queries are routed even without the router.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import DEFAULT_DB_ALIAS

from bi_admin.conf import setting

_active: ContextVar[str | None] = ContextVar("gozar_reports_database", default=None)


def reports_database() -> str:
    return setting("REPORTS_DATABASE") or DEFAULT_DB_ALIAS


def current() -> str | None:
    """The alias reads are routed to right now (``None`` outside ``reporting()``)."""
    return _active.get()


@contextmanager
def reporting(alias: str | None = None) -> Iterator[str]:
    alias = alias or reports_database()
    token = _active.set(alias)
    try:
        yield alias
    finally:
        _active.reset(token)


class ReportsRouter:
    """Sends reads issued while a report runs to the reports database."""

    def db_for_read(self, model, **hints):
        return _active.get()

    def db_for_write(self, model, **hints):
        return None

    def allow_relation(self, obj1, obj2, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return None
