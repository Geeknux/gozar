"""Statement timeout for report queries.

PostgreSQL: ``SET LOCAL statement_timeout`` (via ``set_config(..., true)``) in a
transaction around the report's queries, so the server cancels them. SQLite: a
progress handler that interrupts the running statement once the deadline has
passed. Other backends run without a timeout. Exceeding it raises
``ReportError(504, "statement_timeout")``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import OperationalError, connections, transaction
from django.utils.translation import gettext as _

from bi_admin.conf import setting
from bi_admin.engine.errors import ReportError

SQLITE_CHECK_EVERY = 1000  # SQLite VM instructions between deadline checks
PG_QUERY_CANCELED = "57014"


def timeout_ms() -> int:
    return int(setting("REPORT_STATEMENT_TIMEOUT_MS") or 0)


def timeout_error(ms: int) -> ReportError:
    return ReportError(
        504,
        "statement_timeout",
        _("The report took longer than %(seconds)s seconds and was stopped.")
        % {"seconds": f"{ms / 1000:g}"},
        timeout_ms=ms,
    )


def _sqlstate(exc: BaseException) -> str | None:
    cause = exc.__cause__ or exc
    return getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)


@contextmanager
def statement_timeout(alias: str, ms: int | None = None) -> Iterator[None]:
    """Limit every statement run on ``alias`` inside the block to ``ms``
    milliseconds (default ``GOZAR["REPORT_STATEMENT_TIMEOUT_MS"]``; 0: none)."""
    ms = timeout_ms() if ms is None else int(ms)
    connection = connections[alias]
    if ms <= 0 or connection.vendor not in ("postgresql", "sqlite"):
        yield
        return
    if connection.vendor == "postgresql":
        with transaction.atomic(using=alias):
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_setting('statement_timeout')")
                previous = cursor.fetchone()[0]
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", [str(ms)])
            try:
                yield
            except OperationalError as exc:
                if _sqlstate(exc) == PG_QUERY_CANCELED:
                    raise timeout_error(ms) from exc
                raise
            # SET LOCAL lasts until the outer transaction ends; restore it for
            # whatever else runs in that transaction.
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", [previous])
        return
    connection.ensure_connection()
    raw = connection.connection
    deadline = time.monotonic() + ms / 1000
    raw.set_progress_handler(lambda: int(time.monotonic() > deadline), SQLITE_CHECK_EVERY)
    try:
        yield
    except OperationalError as exc:
        if "interrupted" in str(exc):
            raise timeout_error(ms) from exc
        raise
    finally:
        raw.set_progress_handler(None, 0)
