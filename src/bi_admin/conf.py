"""``settings.GOZAR`` lookups with Gozar's defaults (every key is optional)."""

from __future__ import annotations

from typing import Any

from django.conf import settings

DEFAULTS: dict[str, Any] = {
    "API_DOCS": "staff",  # who may open <api>/docs and openapi.json: "staff" | "public"
    # data management (M6.5)
    "UPLOAD_MAX_BYTES": 10 * 1024 * 1024,  # per uploaded file; 0/None: no limit
    "CRUD_EXPORT_MAX_ROWS": 50_000,  # rows in a list export (the rest is cut off)
    "CRUD_IMPORT_MAX_ROWS": 5_000,  # rows in one import file
    "BULK_EDIT_MAX_ROWS": 1_000,  # rows changed by one bulk edit or select-all action
    "GLOBAL_SEARCH_PER_MODEL": 5,  # hits listed per model in the global search
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
    "JOB_LEASE_SECONDS": 60,  # a running job without a heartbeat for this long is stale
    "JOB_MAX_ATTEMPTS": 3,  # stale jobs are requeued until then, then marked failed
    # scheduled e-mails (M6)
    "EMAIL_FROM": None,  # sender; None: settings.DEFAULT_FROM_EMAIL
    "EMAIL_MAX_ROWS": 50,  # rows per report in the e-mail body (attachments have all)
    "BASE_URL": None,  # e.g. "https://bi.example.com/gozar": adds "Open in Gozar" links
    "WORKER_SENDS_SCHEDULES": True,  # gozar_worker also sends due e-mail schedules
    # System page (M6.5): seconds its database statistics and checks stay cached
    "SYSTEM_CACHE_TIMEOUT": 60,
}


def setting(name: str) -> Any:
    config = getattr(settings, "GOZAR", None) or {}
    return config.get(name, DEFAULTS.get(name))
