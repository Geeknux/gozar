"""Everything else on the System page: versions, apps, cache, job queue, users and
sessions, activity, report usage, e-mail deliveries, media storage, migrations
and Django's system checks. Each function returns JSON-ready data."""

from __future__ import annotations

import datetime as dt
import os
import platform
import time
import uuid
from typing import Any

import django
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import checks
from django.core.cache import caches
from django.core.files.storage import FileSystemStorage, default_storage
from django.db import DatabaseError, connections, models
from django.db.models import Avg, Count, Max, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

import bi_admin
from bi_admin.conf import setting
from bi_admin.models import ChangeLog, ReportJob, ReportLog, ScheduleDelivery

ACTIVITY_DAYS = 30
REPORT_DAYS = 30
TOP_REPORTS = 10
DELIVERIES = 15
MEDIA_SCAN_MAX_FILES = 50_000  # stop walking MEDIA_ROOT after this many files
MEDIA_REFERENCED_MAX = 2_000  # files sized one by one on non-filesystem storages
LEVELS = {
    checks.DEBUG: "DEBUG",
    checks.INFO: "INFO",
    checks.WARNING: "WARNING",
    checks.ERROR: "ERROR",
    checks.CRITICAL: "CRITICAL",
}


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def versions() -> dict[str, str]:
    return {
        "gozar": bi_admin.__version__,
        "django": django.get_version(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
    }


def installed_apps() -> list[dict[str, str]]:
    return [
        {"name": a.name, "label": a.label, "verbose_name": str(a.verbose_name)}
        for a in apps.get_app_configs()
    ]


def cache_backends() -> list[dict[str, Any]]:
    """Alias and backend class only: LOCATION may carry credentials (Redis URLs)."""
    report_alias = setting("REPORT_CACHE")
    return [
        {
            "alias": alias,
            "backend": conf.get("BACKEND", ""),
            "reports": alias == report_alias,
        }
        for alias, conf in settings.CACHES.items()
    ]


def cache_check() -> dict[str, Any]:
    """Write, read and delete a random key in the report cache (live, never cached)."""
    alias = setting("REPORT_CACHE")
    key = f"gozar:system:check:{uuid.uuid4().hex}"
    value = uuid.uuid4().hex
    started = time.monotonic()
    try:
        cache = caches[alias]
        cache.set(key, value, 30)
        hit = cache.get(key) == value
        cache.delete(key)
        error = None
    except Exception as exc:  # any backend failure is the answer here
        hit, error = False, exc.__class__.__name__
    return {
        "alias": alias,
        "backend": settings.CACHES.get(alias, {}).get("BACKEND", ""),
        "ok": hit,
        "error": error,
        "duration_ms": round((time.monotonic() - started) * 1000, 2),
    }


def job_queue(now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or timezone.now()
    stale_before = now - dt.timedelta(seconds=int(setting("JOB_LEASE_SECONDS") or 60))
    S = ReportJob.Status
    counts = dict(ReportJob.objects.values_list("status").annotate(n=Count("pk")).order_by())
    stale = ReportJob.objects.filter(status=S.RUNNING).filter(
        Q(heartbeat_at__lt=stale_before) | Q(heartbeat_at__isnull=True, started_at__lt=stale_before)
    )
    agg = ReportJob.objects.aggregate(
        heartbeat=Max("heartbeat_at"),
        oldest_queued=models.Min("created_at", filter=Q(status=S.QUEUED)),
    )
    return {
        "queued": counts.get(S.QUEUED, 0),
        "running": counts.get(S.RUNNING, 0),
        "done": counts.get(S.DONE, 0),
        "failed": counts.get(S.FAILED, 0),
        "cancelled": counts.get(S.CANCELLED, 0),
        "stale": stale.count(),
        "last_heartbeat": _iso(agg["heartbeat"]),
        "oldest_queued": _iso(agg["oldest_queued"]),
        "lease_seconds": int(setting("JOB_LEASE_SECONDS") or 60),
    }


def users(now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or timezone.now()
    User = get_user_model()
    fields = {f.name for f in User._meta.get_fields()}
    agg = User.objects.aggregate(
        total=Count("pk"),
        **({"active": Count("pk", filter=Q(is_active=True))} if "is_active" in fields else {}),
        **({"staff": Count("pk", filter=Q(is_staff=True))} if "is_staff" in fields else {}),
        **(
            {"superusers": Count("pk", filter=Q(is_superuser=True))}
            if "is_superuser" in fields
            else {}
        ),
    )
    return {**{"active": None, "staff": None, "superusers": None}, **agg, **sessions(now)}


def sessions(now: dt.datetime) -> dict[str, Any]:
    engine = getattr(settings, "SESSION_ENGINE", "")
    if not apps.is_installed("django.contrib.sessions") or engine not in (
        "django.contrib.sessions.backends.db",
        "django.contrib.sessions.backends.cached_db",
    ):
        return {"sessions": None, "session_engine": engine}
    from django.contrib.sessions.models import Session

    try:
        count = Session.objects.filter(expire_date__gt=now).count()
    except DatabaseError:
        count = None
    return {"sessions": count, "session_engine": engine}


def activity(days: int = ACTIVITY_DAYS, now: dt.datetime | None = None) -> dict[str, Any]:
    """ChangeLog entries per local day and action over the last ``days`` days."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    start = today - dt.timedelta(days=days - 1)
    since = timezone.make_aware(dt.datetime.combine(start, dt.time.min))
    rows = (
        ChangeLog.objects.filter(timestamp__gte=since)
        .annotate(day=TruncDate("timestamp", tzinfo=timezone.get_current_timezone()))
        .values("day", "action")
        .annotate(n=Count("pk"))
        .order_by()
    )
    actions = [a.value for a in ChangeLog.Action]
    buckets = {start + dt.timedelta(days=i): dict.fromkeys(actions, 0) for i in range(days)}
    for row in rows:
        if row["day"] in buckets and row["action"] in actions:
            buckets[row["day"]][row["action"]] += row["n"]
    series = [{"date": d.isoformat(), **counts} for d, counts in sorted(buckets.items())]
    return {
        "days": days,
        "series": series,
        "totals": {a: sum(day[a] for day in series) for a in actions},
        "actions": [{"value": a.value, "label": str(a.label)} for a in ChangeLog.Action],
    }


def reports(days: int = REPORT_DAYS, now: dt.datetime | None = None) -> dict[str, Any]:
    """The most used and the slowest reports of the last ``days`` days, per semantic
    model and kind (run, export, drill-down, e-mail)."""
    now = now or timezone.now()
    logs = ReportLog.objects.filter(timestamp__gte=now - dt.timedelta(days=days))
    grouped = list(
        logs.values("semantic_model", "kind")
        .annotate(
            count=Count("pk"),
            failed=Count("pk", filter=~Q(status=ReportLog.Status.OK)),
            cache_hits=Count("pk", filter=Q(cache_hit=True)),
            avg_ms=Avg("duration_ms"),
            max_ms=Max("duration_ms"),
            last=Max("timestamp"),
        )
        .order_by()
    )
    kinds = dict(ReportLog.Kind.choices)
    for g in grouped:
        g["kind_display"] = str(kinds.get(g["kind"], g["kind"]))
        g["avg_ms"] = round(g["avg_ms"]) if g["avg_ms"] is not None else None
        g["last"] = _iso(g["last"])
    most_used = sorted(grouped, key=lambda g: (-g["count"], g["semantic_model"], g["kind"]))
    slowest = sorted(
        (g for g in grouped if g["avg_ms"] is not None),
        key=lambda g: (-g["avg_ms"], g["semantic_model"], g["kind"]),
    )
    return {
        "days": days,
        "total": sum(g["count"] for g in grouped),
        "failed": sum(g["failed"] for g in grouped),
        "most_used": most_used[:TOP_REPORTS],
        "slowest": slowest[:TOP_REPORTS],
    }


def deliveries(limit: int = DELIVERIES) -> list[dict[str, Any]]:
    return [
        {
            "id": d.pk,
            "timestamp": d.timestamp.isoformat(),
            "schedule": d.schedule_name,
            "schedule_id": d.schedule_id,
            "user": d.username or None,
            "status": d.status,
            "status_display": d.get_status_display(),
            "trigger": d.trigger,
            "error": d.error[:300],
        }
        for d in ScheduleDelivery.objects.order_by("-timestamp", "-id")[:limit]
    ]


def media() -> dict[str, Any]:
    """Bytes and files in the default storage: a walk of MEDIA_ROOT for the
    filesystem storage, otherwise the files referenced by file fields."""
    storage = default_storage
    backend = f"{storage.__class__.__module__}.{storage.__class__.__qualname__}"
    out: dict[str, Any] = {
        "backend": backend,
        "url": getattr(settings, "MEDIA_URL", None) or None,
        "method": None,
        "bytes": None,
        "files": None,
        "truncated": False,
    }
    if isinstance(storage, FileSystemStorage):
        root = storage.location
        total = files = 0
        truncated = False
        if os.path.isdir(root):
            for dirpath, _dirs, names in os.walk(root):
                for name in names:
                    if files >= MEDIA_SCAN_MAX_FILES:
                        truncated = True
                        break
                    try:
                        total += os.path.getsize(os.path.join(dirpath, name))
                    except OSError:
                        continue
                    files += 1
                if truncated:
                    break
        return {
            **out,
            "method": "filesystem",
            "bytes": total,
            "files": files,
            "truncated": truncated,
        }
    names: set[str] = set()
    for model in apps.get_models():
        for f in model._meta.concrete_fields:
            if isinstance(f, models.FileField) and f.storage is storage:
                try:
                    names.update(
                        model._base_manager.exclude(**{f.attname: ""})
                        .exclude(**{f"{f.attname}__isnull": True})
                        .values_list(f.attname, flat=True)[: MEDIA_REFERENCED_MAX + 1]
                    )
                except DatabaseError:
                    continue
    truncated = len(names) > MEDIA_REFERENCED_MAX
    total = 0
    for name in sorted(names)[:MEDIA_REFERENCED_MAX]:
        try:
            total += storage.size(name)
        except Exception:  # missing file, or a storage without size()
            continue
    return {
        **out,
        "method": "referenced",
        "bytes": total,
        "files": min(len(names), MEDIA_REFERENCED_MAX),
        "truncated": truncated,
    }


def migrations() -> list[dict[str, Any]]:
    """Unapplied migrations per database alias (default, plus the reports database)."""
    from django.db.migrations.executor import MigrationExecutor

    from bi_admin.system.db import database_alias

    out = []
    for alias in dict.fromkeys(["default", database_alias()]):
        entry: dict[str, Any] = {"alias": alias, "unapplied": [], "conflicts": [], "error": None}
        try:
            executor = MigrationExecutor(connections[alias])
            plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
            entry["unapplied"] = [
                {"app": m.app_label, "name": m.name} for m, backwards in plan if not backwards
            ]
            entry["applied"] = len(executor.loader.applied_migrations)
            entry["conflicts"] = [
                {"app": app, "names": sorted(names)}
                for app, names in executor.loader.detect_conflicts().items()
            ]
        except DatabaseError as exc:
            entry["error"] = exc.__class__.__name__
        out.append(entry)
    return out


def system_checks() -> dict[str, Any]:
    """Django's system checks, the deployment checks included (marked as such)."""
    from bi_admin.system.db import database_alias

    databases = list(dict.fromkeys(["default", database_alias()]))

    def key(m):
        return (m.id, m.msg, str(m.obj))

    regular = {key(m) for m in checks.run_checks(databases=databases)}
    messages = checks.run_checks(include_deployment_checks=True, databases=databases)
    items = [
        {
            "level": m.level,
            "level_name": LEVELS.get(m.level, str(m.level)),
            "id": m.id,
            "message": str(m.msg),
            "hint": str(m.hint) if m.hint else None,
            "object": str(m.obj) if m.obj is not None else None,
            "deployment": key(m) not in regular,
        }
        for m in messages
    ]
    items.sort(key=lambda i: (-i["level"], i["id"] or "", i["message"]))
    counts: dict[str, int] = {}
    for i in items:
        counts[i["level_name"]] = counts.get(i["level_name"], 0) + 1
    return {"checks": items, "counts": counts, "debug": bool(settings.DEBUG)}
