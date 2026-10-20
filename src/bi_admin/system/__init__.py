"""The System page: database statistics, migrations, checks, job queue and usage.

Only for superusers and users with ``bi_admin.view_system``. The expensive parts
are cached for ``GOZAR["SYSTEM_CACHE_TIMEOUT"]`` seconds in the report cache;
``refresh=True`` recomputes them. Statistics are read on the reports database
when ``GOZAR["REPORTS_DATABASE"]`` is set."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.core.cache import caches
from django.utils import timezone, translation

from bi_admin.conf import setting
from bi_admin.system import db, info

PERMISSION = "bi_admin.view_system"


def can_view(user) -> bool:
    return bool(user.is_active and user.is_staff and user.has_perm(PERMISSION))


def cached(section: str, compute: Callable[[], dict[str, Any]], refresh: bool = False) -> dict:
    """``compute()`` plus ``generated_at``, kept briefly in the report cache.
    The key has the language (labels are translated) and the statistics alias."""
    timeout = int(setting("SYSTEM_CACHE_TIMEOUT") or 0)
    lang = translation.get_language() or ""
    key = f"gozar:system:{section}:{db.database_alias()}:{lang}"
    cache = caches[setting("REPORT_CACHE")]
    if timeout > 0 and not refresh:
        hit = cache.get(key)
        if hit is not None:
            return {**hit, "cached": True}
    data = {**compute(), "generated_at": timezone.now().isoformat()}
    if timeout > 0:
        cache.set(key, data, timeout)
    return {**data, "cached": False}


def database(refresh: bool = False) -> dict[str, Any]:
    return cached("database", db.database_stats, refresh)


def overview(refresh: bool = False) -> dict[str, Any]:
    def compute():
        stats = database(refresh)
        summary = {k: v for k, v in stats.items() if k not in ("tables", "cached", "generated_at")}
        return {
            "database": summary,
            "versions": info.versions(),
            "apps": info.installed_apps(),
            "caches": info.cache_backends(),
            "users": info.users(),
            "activity": info.activity(),
            "reports": info.reports(),
            "deliveries": info.deliveries(),
            "media": info.media(),
            "migrations": info.migrations(),
        }

    out = cached("overview", compute, refresh)
    # Live on every call: the cache check and the job queue are cheap and change fast.
    out["cache_check"] = info.cache_check()
    out["jobs"] = info.job_queue()
    return out


def system_checks(refresh: bool = False) -> dict[str, Any]:
    return cached("checks", info.system_checks, refresh)
