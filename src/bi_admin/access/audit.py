"""Audit log of report executions (``bi_admin.ReportLog``).

Every run, export and drill-down — through the API, a background job or code
that passes a request — is recorded with the user, the normalised spec, the
row-level scope, the duration, the row count, the outcome and the error code,
including refused and failed ones. ``GOZAR["AUDIT_REPORTS"] = False`` turns it
off.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from django.db import DEFAULT_DB_ALIAS, transaction

from bi_admin.conf import setting
from bi_admin.engine.errors import ReportError
from bi_admin.models import ReportLog

logger = logging.getLogger("bi_admin.audit")


@dataclass
class Entry:
    kind: str
    semantic_model: str
    spec: dict[str, Any]
    scope: str = ""
    database: str = ""
    row_count: int | None = None
    cache_hit: bool = False
    skip: bool = False  # set when nothing was executed (a cache-only lookup missed)
    status: str = ReportLog.Status.OK
    error_code: str = ""
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@contextmanager
def record(kind: str, spec, request, job=None) -> Iterator[Entry]:
    """Audit one execution; the block fills in the entry as it learns more."""
    from bi_admin.engine.validator import SpecValidationError

    entry = Entry(kind, spec.semantic_model, spec.model_dump(mode="json"))
    started = time.monotonic()
    try:
        yield entry
    except SpecValidationError as exc:
        entry.status = ReportLog.Status.INVALID
        entry.error_code = exc.codes[0] if exc.codes else "invalid_spec"
        entry.error = str(exc)
        raise
    except ReportError as exc:
        entry.status = ReportLog.Status.DENIED if exc.status == 403 else ReportLog.Status.FAILED
        entry.error_code = exc.code
        entry.error = str(exc)
        raise
    except Exception as exc:
        entry.status = ReportLog.Status.FAILED
        entry.error_code = "internal_error"
        entry.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _write(entry, request, job, int((time.monotonic() - started) * 1000))


def _write(entry: Entry, request, job, duration_ms: int) -> None:
    if entry.skip or not setting("AUDIT_REPORTS"):
        return
    user = getattr(request, "user", None) if request is not None else None
    if user is not None and not user.is_authenticated:
        user = None
    try:
        with transaction.atomic(using=DEFAULT_DB_ALIAS):  # a failed insert stays contained
            _create(entry, user, job, duration_ms)
    except Exception:  # never let auditing hide the report's own outcome
        logger.exception("Could not write the report audit log entry")


def _create(entry: Entry, user, job, duration_ms: int) -> None:
    ReportLog.objects.using(DEFAULT_DB_ALIAS).create(
        user=user,
        username=user.get_username() if user is not None else "",
        kind=entry.kind,
        semantic_model=entry.semantic_model[:100],
        spec=entry.spec,
        scope=entry.scope,
        status=entry.status,
        error_code=entry.error_code[:64],
        error=entry.error[:2000],
        duration_ms=duration_ms,
        row_count=entry.row_count,
        cache_hit=entry.cache_hit,
        database=entry.database,
        job=job,
    )
