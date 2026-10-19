"""``settings.GOZAR`` lookups with Gozar's defaults (every key is optional)."""

from __future__ import annotations

from typing import Any

from django.conf import settings

DEFAULTS: dict[str, Any] = {
    # access control (M4)
    "ROW_RULES_BYPASS_SUPERUSER": True,
    "AUDIT_REPORTS": True,
    # limits
    "REPORT_MAX_ROWS": 10_000,
    "REPORT_MAX_DIMENSIONS": 6,
    "REPORT_MAX_FILTERS": 20,
    "REPORT_STATEMENT_TIMEOUT_MS": 30_000,  # 0/None: no timeout
    # execution
    "REPORTS_DATABASE": None,  # e.g. "replica"; None: the default database
    "REPORT_CACHE": "default",  # cache alias (django.core.cache.caches)
    "REPORT_CACHE_TIMEOUT": 300,  # seconds; 0 disables the result cache
    "REPORT_ASYNC": False,  # run reports as background jobs by default
    "JOB_BACKEND": "bi_admin.execution.jobs.DatabaseBackend",
}


def setting(name: str) -> Any:
    config = getattr(settings, "GOZAR", None) or {}
    return config.get(name, DEFAULTS.get(name))
