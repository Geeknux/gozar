"""Operational alerts, delivered to Telegram (``GOZAR["ALERTS"]``).

Public API — all of it safe to call from anywhere, it never raises::

    from bi_admin import alerts

    alerts.raise_alert("jobs", "nightly-import", "error", "Nightly import failed", str(exc))
    alerts.resolve("jobs", "nightly-import", "Nightly import works again")

Modules: ``conf`` (settings), ``sources`` (switches and minimum severities),
``pipeline`` (dedup, rate limits, quiet hours, mutes), ``outbox`` (sending with
retries, in ``gozar_worker``), ``telegram`` (the channel and its allow-list),
``messages`` (HTML texts), ``handlers`` (the logging handler), ``hooks`` and
``monitors`` (the sources), ``bot`` (interactive commands), ``scrub`` (secrets).
"""

from __future__ import annotations

from typing import Any

__all__ = ["acknowledge", "raise_alert", "resolve", "send_test", "status"]


# Thin wrappers, so importing the package (``LOGGING`` naming the handler, before
# the app registry is ready) doesn't import the models.
def raise_alert(*args, **kwargs):
    from bi_admin.alerts import pipeline

    return pipeline.raise_alert(*args, **kwargs)


def resolve(*args, **kwargs):
    from bi_admin.alerts import pipeline

    return pipeline.resolve(*args, **kwargs)


def acknowledge(*args, **kwargs):
    from bi_admin.alerts import pipeline

    return pipeline.acknowledge(*args, **kwargs)


def send_test(user=None, message: str = "", severity: str = "info"):
    """Queue a test alert (the System page's button, ``gozar_alert_test``)."""
    from django.utils import translation
    from django.utils.translation import gettext as _

    from bi_admin.alerts.pipeline import language

    with translation.override(language()):
        title = _("Test alert from Gozar")
        if user is not None:
            title += f" ({user.get_username()})"
        text = message or _("If you can read this, alerts reach this chat.")
    # the test source's window is 0: test alerts are never folded into an earlier one
    return raise_alert(
        "test", "manual", severity, title, text, {"user_id": getattr(user, "pk", None)}
    )


def status() -> dict[str, Any]:
    """Channel, sources, outbox and worker for the System page (never the token
    or the allowed ids)."""
    from bi_admin.alerts import monitors, outbox, sources, telegram
    from bi_admin.alerts.conf import alert_setting
    from bi_admin.alerts.pipeline import language, quiet_hours

    overrides = sources.overrides()
    quiet = quiet_hours()
    return {
        "enabled": bool(alert_setting("ENABLED")),
        "language": language(),
        "window": int(alert_setting("WINDOW") or 0),
        "quiet_hours": (
            {
                "start": quiet[0].strftime("%H:%M"),
                "end": quiet[1].strftime("%H:%M"),
                "timezone": str(quiet[2]),
            }
            if quiet
            else None
        ),
        "channels": [telegram.config().status()],
        "sources": [
            {"name": name, "label": str(src.label), **sources.effective(name, overrides)}
            for name, src in sources.SOURCES.items()
        ],
        "severities": list(sources.SEVERITIES),
        "outbox": outbox.stats(),
        "worker": monitors.worker_status(),
    }
