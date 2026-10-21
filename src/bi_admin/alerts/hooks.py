"""Alert sources called from Gozar's own code paths. Every function here is safe
to call anywhere: it never raises and only writes alert rows (sending happens in
the worker)."""

from __future__ import annotations

import functools
import logging
from typing import Any

from django.core.cache import caches
from django.utils.translation import gettext as _

from bi_admin.alerts import scrub
from bi_admin.alerts.conf import alert_setting
from bi_admin.alerts.pipeline import raise_alert, resolve

logger = logging.getLogger("bi_admin.alerts")


def safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.warning("Alert hook %s failed", fn.__name__, exc_info=True)
            return None

    return wrapper


def count(name: str, window: float) -> int:
    """A fixed-window counter in the report cache (shared by every process when
    the cache is)."""
    from bi_admin.conf import setting

    cache = caches[setting("REPORT_CACHE")]
    key = f"gozar:alerts:count:{name}"
    cache.add(key, 0, int(window) or 1)
    try:
        return int(cache.incr(key))
    except ValueError:  # expired between add and incr
        cache.set(key, 1, int(window) or 1)
        return 1


def peek(name: str) -> int:
    from bi_admin.conf import setting

    return int(caches[setting("REPORT_CACHE")].get(f"gozar:alerts:count:{name}") or 0)


# --- jobs -----------------------------------------------------------------------------


@safe
def job_failed(job, error: dict[str, Any] | None, status: int | None) -> None:
    """A background job ended in a server-side failure (5xx). Refused or invalid
    reports (403/422) are the user's business, not an operational alert."""
    if status is None or status < 500:
        return
    code = (error or {}).get("code") or "internal_error"
    kind = getattr(job, "kind", "report")
    target = (job.options or {}).get("target") or job.semantic_model
    severity = "warning" if code == "statement_timeout" else "error"
    title = _("Background job failed: %(kind)s %(target)s") % {"kind": kind, "target": target}
    message = scrub.text((error or {}).get("detail") or code, 500)
    raise_alert(
        "jobs",
        f"{kind}:{target}:{code}",
        severity,
        title,
        message,
        {
            "job": str(job.pk),
            "kind": kind,
            "code": code,
            "status": status,
            "attempts": job.attempts,
        },
    )


@safe
def job_lost(job, attempts: int) -> None:
    kind = getattr(job, "kind", "report")
    title = _("Job lost: its worker stopped responding (%(kind)s)") % {"kind": kind}
    message = _("Job %(id)s failed after %(n)s attempts (worker_lost).") % {
        "id": job.pk,
        "n": attempts,
    }
    raise_alert(
        "jobs",
        f"worker_lost:{kind}",
        "error",
        title,
        message,
        {"job": str(job.pk), "kind": kind, "attempts": attempts},
    )


# --- scheduled e-mails ----------------------------------------------------------------


@safe
def schedule_deliveries(schedule, deliveries) -> None:
    """One alert per send of a schedule with failed recipients; the next send
    without failures resolves it."""
    failed = [d for d in deliveries if d.status == "failed"]
    key = f"schedule:{schedule.pk}"
    name = deliveries[0].schedule_name if deliveries else str(schedule.pk)
    if not failed:
        if any(d.status == "sent" for d in deliveries):
            resolve(
                "schedules", key, _("E-mail schedule delivered again: %(name)s") % {"name": name}
            )
        return
    title = _("E-mail schedule failed: %(name)s") % {"name": name}
    lines = [
        _("Recipient id %(user)s: %(error)s")
        % {"user": d.recipient_id, "error": scrub.text(d.error, 300)}
        for d in failed[:5]
    ]
    raise_alert(
        "schedules",
        key,
        "warning",
        title,
        "\n".join(lines),
        {
            "schedule": schedule.pk,
            "failed": len(failed),
            "recipients": len(deliveries),
            "trigger": failed[0].trigger,
        },
    )


# --- pre-aggregations and columnar copies -----------------------------------------------


@safe
def refresh_failed(name: str, exc: BaseException, mode: str = "") -> None:
    title = _("Pre-aggregation refresh failed: %(name)s") % {"name": name}
    raise_alert(
        "preaggregations",
        f"{name}:failed",
        "error",
        title,
        scrub.text(f"{type(exc).__name__}: {exc}", 800),
        {"mode": mode},
    )


@safe
def refresh_succeeded(name: str) -> None:
    resolve(
        "preaggregations",
        f"{name}:failed",
        _("Pre-aggregation refreshed again: %(name)s") % {"name": name},
    )


@safe
def sync_failed(name: str, exc: BaseException) -> None:
    title = _("Columnar sync failed: %(name)s") % {"name": name}
    raise_alert(
        "columnar",
        f"{name}:failed",
        "error",
        title,
        scrub.text(f"{type(exc).__name__}: {exc}", 800),
    )


@safe
def sync_succeeded(name: str) -> None:
    resolve(
        "columnar", f"{name}:failed", _("Columnar copy synced again: %(name)s") % {"name": name}
    )


# --- language model -------------------------------------------------------------------


@safe
def nl_provider_error(provider: str, code: str, fell_back: bool) -> None:
    threshold = int(alert_setting("NL_ERRORS") or 0)
    if threshold <= 0:
        return
    window = float(alert_setting("NL_ERRORS_WINDOW") or 600)
    n = count("nl", window)
    if n < threshold:
        return
    title = _("Language model errors: %(n)s in %(minutes)s minutes") % {
        "n": n,
        "minutes": max(1, int(window // 60)),
    }
    if fell_back:
        message = _("Provider %(provider)s failed (%(code)s); the built-in interpreter answered.")
    else:
        message = _("Provider %(provider)s failed (%(code)s); questions got an error.")
    raise_alert(
        "nl",
        "provider",
        "warning",
        title,
        message % {"provider": provider, "code": code},
        {"provider": provider, "code": code, "fallback": fell_back, "count": n},
    )


# --- logins ---------------------------------------------------------------------------


def client_ip(request) -> str:
    meta = getattr(request, "META", None) or {}
    header = alert_setting("CLIENT_IP_HEADER")
    if header and meta.get(header):
        return str(meta[header]).split(",")[0].strip()[:64]
    return str(meta.get("REMOTE_ADDR") or "unknown")[:64]


@safe
def login_failed(request) -> None:
    """A failed staff login (bad credentials, or an account without admin access).
    Neither the username nor the password goes into the alert."""
    threshold = int(alert_setting("FAILED_LOGINS") or 0)
    if threshold <= 0 or request is None:
        return
    ip = client_ip(request)
    window = float(alert_setting("FAILED_LOGINS_WINDOW") or 600)
    n = count(f"logins:{ip}", window)
    if n < threshold:
        return
    title = _("Repeated failed logins from %(ip)s") % {"ip": ip}
    message = _("%(n)s failed logins within %(minutes)s minutes.") % {
        "n": n,
        "minutes": max(1, int(window // 60)),
    }
    raise_alert(
        "logins",
        ip,
        "warning",
        title,
        message,
        {"ip": ip, "count": n, "path": getattr(request, "path", "")},
    )


def _on_login_failed(sender, credentials=None, request=None, **kwargs) -> None:
    if getattr(request, "_gozar_login_failure_counted", False):
        return
    if request is not None:
        request._gozar_login_failure_counted = True
    login_failed(request)


def connect() -> None:
    from django.contrib.auth.signals import user_login_failed

    user_login_failed.connect(_on_login_failed, dispatch_uid="gozar_alerts_login_failed")
