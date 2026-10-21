"""Alert sources: each can be switched off and given a minimum severity, in
``GOZAR["ALERTS"]["SOURCES"]`` (defaults) and on the System page (overrides,
``bi_admin.AlertSource``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.utils.translation import gettext_lazy as _

SEVERITIES = ("info", "warning", "error", "critical")


def rank(severity: str) -> int:
    return SEVERITIES.index(severity) if severity in SEVERITIES else 0


@dataclass(frozen=True)
class Source:
    name: str
    label: Any
    min_severity: str = "warning"
    window: int | None = None  # seconds; None: GOZAR["ALERTS"]["WINDOW"]
    enabled: bool = True


SOURCES: dict[str, Source] = {
    s.name: s
    for s in [
        Source("exceptions", _("Unhandled exceptions and HTTP 500 responses"), "error"),
        Source("jobs", _("Background job failures and lost workers"), "warning"),
        Source("worker", _("Worker heartbeat"), "warning", 3600),
        Source("schedules", _("Failed e-mail schedule deliveries"), "warning", 3600),
        Source(
            "preaggregations", _("Pre-aggregation refresh failures and staleness"), "warning", 3600
        ),
        Source(
            "columnar", _("DuckDB (columnar engine) sync failures and staleness"), "warning", 3600
        ),
        Source("nl", _("Language model errors and fallbacks"), "warning", 3600),
        Source("checks", _("Django system and deployment checks"), "warning", 86_400),
        Source("database", _("Database size, table growth and disk usage"), "warning", 21_600),
        Source("logins", _("Repeated failed logins from one IP address"), "warning", 3600),
        Source("telegram", _("Unauthorised use of the Telegram bot"), "warning", 3600),
        Source("test", _("Test alerts"), "info", 0),
    ]
}


def effective(name: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """``{"enabled", "min_severity", "window", "overridden"}`` of one source: the
    System page override, else ``GOZAR["ALERTS"]["SOURCES"]``, else the default."""
    from bi_admin.alerts.conf import alert_setting

    source = SOURCES.get(name) or Source(name, name)
    configured = (alert_setting("SOURCES") or {}).get(name) or {}
    enabled = configured.get("enabled", source.enabled)
    minimum = configured.get("min_severity", source.min_severity)
    window = configured.get("window", source.window)
    if window is None:
        window = alert_setting("WINDOW")
    out = {
        "enabled": bool(enabled),
        "min_severity": minimum if minimum in SEVERITIES else source.min_severity,
        "window": int(window or 0),
        "overridden": False,
        "default_enabled": bool(enabled),
        "default_min_severity": minimum if minimum in SEVERITIES else source.min_severity,
    }
    override = (overrides or {}).get(name)
    if override is not None:
        if override.enabled is not None:
            out["enabled"], out["overridden"] = override.enabled, True
        if override.min_severity in SEVERITIES:
            out["min_severity"], out["overridden"] = override.min_severity, True
    return out


def overrides() -> dict[str, Any]:
    from django.db import DEFAULT_DB_ALIAS

    from bi_admin.models import AlertSource

    return {o.source: o for o in AlertSource.objects.using(DEFAULT_DB_ALIAS).all()}
