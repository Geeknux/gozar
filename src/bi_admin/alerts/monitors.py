"""Periodic condition checks: run by ``gozar_worker`` every
``GOZAR["ALERTS"]["CHECK_EVERY"]`` seconds and by ``manage.py gozar_alerts
--check`` (cron — the watchdog that notices a dead worker). Each condition raises
an alert while it holds and resolves it when it clears.

* ``worker`` — the newest heartbeat of a running ``gozar_worker`` is older than
  ``WORKER_STALE_SECONDS`` (nothing is checked until a worker has ever beaten).
* ``preaggregations`` / ``columnar`` — a copy stale for longer than
  ``max(max_staleness, refresh interval) + STALENESS_GRACE``: the worker should
  have refreshed it by then.
* ``nl`` — the language model error counter's window passed without errors.
* ``database`` — database size above ``DATABASE_MAX_BYTES``, a table that grew by
  more than ``TABLE_GROWTH_PERCENT`` (and ``TABLE_GROWTH_MIN_BYTES``) over
  ``TABLE_GROWTH_PERIOD``, a disk above ``DISK_USAGE_PERCENT``.
* ``checks`` — Django's system checks, deployment checks included: one alert
  listing every error and warning, re-raised when the list changes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import shutil
import socket
from typing import Any

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS
from django.utils import timezone
from django.utils.translation import gettext as _

from bi_admin.alerts import hooks, scrub
from bi_admin.alerts.conf import alert_setting
from bi_admin.alerts.pipeline import firing, firing_keys, raise_alert, resolve
from bi_admin.models import AlertState, WorkerHeartbeat

logger = logging.getLogger("bi_admin.alerts")


def _minutes(seconds: float) -> int:
    return max(1, int(seconds // 60))


# --- worker heartbeat -----------------------------------------------------------------


def worker_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"[:200]


def beat(name: str | None = None, *, started: bool = False, now=None) -> None:
    now = now or timezone.now()
    name = name or worker_name()
    rows = WorkerHeartbeat.objects.using(DEFAULT_DB_ALIAS)
    try:
        if started:
            host, _sep, pid = name.rpartition(":")
            rows.update_or_create(
                name=name,
                defaults={
                    "host": host[:150],
                    "pid": int(pid) if pid.isdigit() else None,
                    "started_at": now,
                    "beat_at": now,
                    "stopped_at": None,
                },
            )
            # older rows of stopped or long-dead workers aren't interesting any more
            rows.filter(beat_at__lt=now - dt.timedelta(days=7)).delete()
            hooks.safe(resolve)("worker", "stale", _("A worker is running again."))
        else:
            rows.filter(name=name).update(beat_at=now, stopped_at=None)
    except Exception:
        logger.warning("Could not record the worker heartbeat", exc_info=True)


def stopped(name: str | None = None) -> None:
    try:
        WorkerHeartbeat.objects.using(DEFAULT_DB_ALIAS).filter(name=name or worker_name()).update(
            stopped_at=timezone.now()
        )
    except Exception:
        logger.warning("Could not record the worker stop", exc_info=True)


def worker_status(now=None) -> dict[str, Any]:
    now = now or timezone.now()
    limit = float(alert_setting("WORKER_STALE_SECONDS") or 300)
    rows = list(WorkerHeartbeat.objects.using(DEFAULT_DB_ALIAS).order_by("-beat_at")[:10])
    running = [w for w in rows if w.stopped_at is None]
    latest = running[0].beat_at if running else None
    return {
        "workers": [
            {
                "name": w.name,
                "started_at": w.started_at.isoformat(),
                "beat_at": w.beat_at.isoformat(),
                "stopped_at": w.stopped_at.isoformat() if w.stopped_at else None,
                "stale": w.stopped_at is None and (now - w.beat_at).total_seconds() > limit,
            }
            for w in rows
        ],
        "last_beat_at": latest.isoformat() if latest else None,
        "stale": bool(running) and (now - latest).total_seconds() > limit,
        "stale_seconds": int(limit),
    }


def check_worker(now=None) -> None:
    now = now or timezone.now()
    status = worker_status(now)
    if status["stale"]:
        age = (now - dt.datetime.fromisoformat(status["last_beat_at"])).total_seconds()
        raise_alert(
            "worker",
            "stale",
            "error",
            _("The worker stopped: no heartbeat for %(minutes)s minutes")
            % {"minutes": _minutes(age)},
            _(
                "Background jobs, e-mail schedules, refreshes and alert messages wait "
                "until gozar_worker runs again."
            ),
            {"last_beat_at": status["last_beat_at"]},
            now=now,
        )
    elif status["last_beat_at"]:
        resolve("worker", "stale", _("The worker is running again."), now=now)


# --- staleness ------------------------------------------------------------------------


def check_staleness(now=None) -> None:
    from bi_admin.models import Materialization
    from bi_admin.scale import columnar
    from bi_admin.scale import state as states
    from bi_admin.scale.preaggregations import all_preaggregations

    now = now or timezone.now()
    grace = float(alert_setting("STALENESS_GRACE") or 0)
    rows = states.states(Materialization.Kind.PREAGGREGATION)
    for preagg in all_preaggregations():
        row = rows.get(preagg.key)
        if row is None or not row.source_versions:
            continue
        limit = max(preagg.setting("MAX_STALENESS"), preagg.setting("REFRESH_EVERY")) + grace
        _stale("preaggregations", preagg.key, states.freshness(row, now), limit, now)
    copies = states.states(Materialization.Kind.COLUMNAR)
    for backend in columnar.in_use():
        row = copies.get(backend.name)
        if row is None or not row.source_versions:
            continue
        from bi_admin.conf import duckdb_setting

        limit = max(float(duckdb_setting("MAX_STALENESS") or 0), float(backend.sync_every()))
        _stale("columnar", backend.name, states.freshness(row, now), limit + grace, now)


def _stale(source: str, name: str, fresh, limit: float, now) -> None:
    key = f"{name}:stale"
    if not fresh.fresh and fresh.lag_seconds is not None and fresh.lag_seconds > limit:
        raise_alert(
            source,
            key,
            "warning",
            _("%(name)s is stale for %(minutes)s minutes")
            % {"name": name, "minutes": _minutes(fresh.lag_seconds)},
            _(
                "Allowed: %(minutes)s minutes. Reports fall back to the database "
                "meanwhile; check gozar_worker and the refresh errors."
            )
            % {"minutes": _minutes(limit)},
            {"lag_seconds": int(fresh.lag_seconds), "limit_seconds": int(limit)},
            now=now,
        )
    elif fresh.fresh:
        resolve(source, key, _("%(name)s is fresh again") % {"name": name}, now=now)


# --- language model ----------------------------------------------------------------------


def check_nl(now=None) -> None:
    if firing("nl", "provider") and hooks.peek("nl") == 0:
        resolve("nl", "provider", _("No language model errors recently."), now=now)


# --- database size, growth, disk ---------------------------------------------------------


def _snapshot_state() -> AlertState:
    state, _created = AlertState.objects.using(DEFAULT_DB_ALIAS).get_or_create(
        key="database:_snapshot", defaults={"source": "database"}
    )
    return state


def check_database(now=None) -> None:
    from bi_admin.system import db

    now = now or timezone.now()
    max_bytes = alert_setting("DATABASE_MAX_BYTES")
    growth = alert_setting("TABLE_GROWTH_PERCENT")
    if max_bytes or growth:
        stats = db.database_stats()
        size = stats.get("total_size")
        if max_bytes and size is not None:
            if size > float(max_bytes):
                raise_alert(
                    "database",
                    "size",
                    "warning",
                    _("The database is larger than the limit"),
                    _("%(size)s MB, limit %(limit)s MB")
                    % {"size": size // 2**20, "limit": int(max_bytes) // 2**20},
                    {"size_bytes": size, "limit_bytes": int(max_bytes)},
                    now=now,
                )
            else:
                resolve(
                    "database", "size", _("The database is below its size limit again"), now=now
                )
        if growth:
            check_growth(stats, float(growth), now)
    check_disks(now)


def check_growth(stats: dict[str, Any], percent: float, now) -> None:
    state = _snapshot_state()
    data = state.data or {}
    sizes = {
        t["name"]: t.get("total_size")
        for t in stats.get("tables", [])
        if t.get("total_size") is not None
    }
    taken = data.get("taken_at")
    period = float(alert_setting("TABLE_GROWTH_PERIOD") or 86_400)
    if not taken:
        state.data = {"taken_at": now.isoformat(), "sizes": sizes}
        state.save(update_fields=["data", "updated_at"])
        return
    elapsed = (now - dt.datetime.fromisoformat(taken)).total_seconds()
    if elapsed < period:
        return
    minimum = float(alert_setting("TABLE_GROWTH_MIN_BYTES") or 0)
    grown = []
    for name, size in sizes.items():
        before = (data.get("sizes") or {}).get(name)
        if before and size - before >= minimum and (size - before) * 100 / before > percent:
            grown.append((name, before, size))
    flagged = set(firing_keys("database"))
    for name, before, size in grown:
        raise_alert(
            "database",
            f"growth:{name}",
            "warning",
            _("Table %(table)s grew by %(percent)s%%")
            % {"table": name, "percent": round((size - before) * 100 / before)},
            _("%(before)s MB → %(after)s MB in %(hours)s hours")
            % {"before": before // 2**20, "after": size // 2**20, "hours": round(elapsed / 3600)},
            {"table": name, "before": before, "after": size},
            now=now,
        )
    for key in flagged:
        if key.startswith("growth:") and key[len("growth:") :] not in {g[0] for g in grown}:
            resolve("database", key, _("Table growth is back to normal"), now=now)
    state.data = {"taken_at": now.isoformat(), "sizes": sizes}
    state.save(update_fields=["data", "updated_at"])


def disk_paths() -> list[str]:
    configured = alert_setting("DISK_PATHS")
    if configured:
        return [str(p) for p in configured]
    paths = []
    if getattr(settings, "MEDIA_ROOT", None):
        paths.append(str(settings.MEDIA_ROOT))
    for conf in settings.DATABASES.values():
        if "sqlite" in conf.get("ENGINE", "") and conf.get("NAME"):
            paths.append(os.path.dirname(os.path.abspath(str(conf["NAME"]))))
    return list(dict.fromkeys(paths))


def _existing(path: str) -> str | None:
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    return path or None


def check_disks(now=None) -> None:
    percent = alert_setting("DISK_USAGE_PERCENT")
    if not percent:
        return
    seen: set[int] = set()
    for configured in disk_paths():
        path = _existing(configured)
        if path is None:
            continue
        try:
            device = os.stat(path).st_dev
            if device in seen:
                continue
            seen.add(device)
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        used = usage.used * 100 / usage.total if usage.total else 0
        key = f"disk:{hashlib.sha256(str(device).encode()).hexdigest()[:8]}"
        label = os.path.basename(path.rstrip("/")) or path
        if used > float(percent):
            raise_alert(
                "database",
                key,
                "error" if used > 97 else "warning",
                _("Disk %(percent)s%% full (%(name)s)") % {"percent": round(used), "name": label},
                _("%(free)s GB free of %(total)s GB")
                % {"free": round(usage.free / 2**30, 1), "total": round(usage.total / 2**30, 1)},
                {"percent": round(used, 1), "free_bytes": usage.free},
                now=now,
            )
        else:
            resolve("database", key, _("Disk usage is below the limit again"), now=now)


# --- Django system checks -------------------------------------------------------------------


def run_system_checks(now=None) -> list[dict[str, Any]]:
    from django.core import checks

    from bi_admin.system.db import database_alias

    databases = list(dict.fromkeys(["default", database_alias()]))
    messages = [
        m
        for m in checks.run_checks(include_deployment_checks=True, databases=databases)
        if m.level >= checks.WARNING
    ]
    found = sorted(
        ({"id": m.id or "", "level": m.level, "message": scrub.text(m.msg, 300)} for m in messages),
        key=lambda m: (-m["level"], m["id"]),
    )
    if not found:
        resolve("checks", "system", _("Django's system checks pass again."), now=now)
        return found
    fingerprint = hashlib.sha256(",".join(sorted(m["id"] for m in found)).encode()).hexdigest()
    state = AlertState.objects.using(DEFAULT_DB_ALIAS).filter(key="checks:system").first()
    if state and state.firing and (state.data or {}).get("fingerprint") != fingerprint:
        resolve("checks", "system", _("The system check results changed."), now=now)
    errors = sum(1 for m in found if m["level"] >= checks.ERROR)
    severity = "error" if errors else "warning"
    title = _("System checks: %(errors)s errors, %(warnings)s warnings") % {
        "errors": errors,
        "warnings": len(found) - errors,
    }
    message = "\n".join(f"{m['id']}: {m['message'][:160]}" for m in found[:15])
    alert = raise_alert(
        "checks", "system", severity, title, message, {"checks": [m["id"] for m in found]}, now=now
    )
    if alert is not None:
        AlertState.objects.using(DEFAULT_DB_ALIAS).filter(key="checks:system").update(
            data={"fingerprint": fingerprint}
        )
    return found


def run(now=None, *, system_checks: bool = False, worker: bool = True) -> None:
    """Every periodic check; each one on its own, so one failing doesn't stop the rest."""
    now = now or timezone.now()
    steps = [check_staleness, check_nl, check_database]
    if worker:
        steps.insert(0, check_worker)
    if system_checks:
        steps.append(run_system_checks)
    for step in steps:
        try:
            step(now)
        except Exception:
            logger.warning("Alert check %s failed", step.__name__, exc_info=True)
