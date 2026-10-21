"""``GOZAR["ALERTS"]`` options and their defaults.

Every key is optional. ``TELEGRAM`` is a nested dict (``TELEGRAM_DEFAULTS``); its
bot token is read **only** from the environment variable named by ``TOKEN_ENV``
(``GOZAR_TELEGRAM_BOT_TOKEN`` by default) — a literal token in settings is system
check error ``bi_admin.E006``. ``ALLOWED_CHAT_IDS`` falls back to the environment
variable ``GOZAR_TELEGRAM_ALLOWED_CHAT_IDS`` when the setting is left out.
"""

from __future__ import annotations

import os
from typing import Any

from django.conf import settings

DEFAULTS: dict[str, Any] = {
    "ENABLED": True,  # record alerts and deliver them through the configured channels
    "LANGUAGE": "en",  # language of alert texts and messages: "en" | "fa"
    "WINDOW": 900,  # seconds: the same key is delivered at most once per window
    # "23:00-07:00" (local times; may wrap midnight): only critical alerts are sent
    # meanwhile, the rest is held until the end of the quiet hours
    "QUIET_HOURS": None,
    "QUIET_HOURS_TIMEZONE": None,  # IANA zone of QUIET_HOURS; None: settings.TIME_ZONE
    # per source: {"enabled": bool, "min_severity": "info|warning|error|critical",
    # "window": seconds}; see bi_admin.alerts.sources for the sources and defaults
    "SOURCES": {},
    "LOG_HANDLER": True,  # attach AlertHandler to the "django.request" logger at startup
    "RELEASE": None,  # shown in exception alerts; None: GOZAR_RELEASE, else the git sha
    "TRACEBACK_FRAMES": 4,  # innermost frames of a traceback included in an alert
    # outbox (sent by gozar_worker)
    "MAX_ATTEMPTS": 6,  # a message that failed this many times is given up
    "RETRY_BACKOFF": 30,  # seconds before the first retry; doubles each time
    "RETRY_BACKOFF_MAX": 3600,
    "DELIVERY_LEASE_SECONDS": 120,  # a message stuck in "sending" this long is retried
    "KEEP_DAYS": 30,  # alerts older than this are purged by the worker (0: keep all)
    # periodic checks (gozar_worker, manage.py gozar_alerts --check)
    "CHECK_EVERY": 300,  # seconds between the condition checks below
    "SYSTEM_CHECKS_EVERY": 21_600,  # seconds between Django system/deployment checks
    "WORKER_STALE_SECONDS": 300,  # a worker silent this long is alerted
    "STALENESS_GRACE": 600,  # seconds past max(max_staleness, refresh interval)
    "DATABASE_MAX_BYTES": None,  # alert above this database size (None: off)
    "TABLE_GROWTH_PERCENT": None,  # alert when a table grew by more than this (None: off)
    "TABLE_GROWTH_MIN_BYTES": 50 * 1024 * 1024,  # … and by at least this many bytes
    "TABLE_GROWTH_PERIOD": 86_400,  # seconds between the sizes compared
    "DISK_USAGE_PERCENT": 90,  # alert above this share of a disk in use (None: off)
    "DISK_PATHS": None,  # paths whose disks are watched; None: MEDIA_ROOT and the SQLite file
    "FAILED_LOGINS": 10,  # failed logins from one IP within the window below (0: off)
    "FAILED_LOGINS_WINDOW": 600,
    "CLIENT_IP_HEADER": None,  # e.g. "HTTP_X_FORWARDED_FOR" behind a trusted proxy
    "NL_ERRORS": 5,  # language model errors or fallbacks within the window below (0: off)
    "NL_ERRORS_WINDOW": 600,
    "TELEGRAM": {},  # see TELEGRAM_DEFAULTS
}

TELEGRAM_DEFAULTS: dict[str, Any] = {
    # None: on when the token and the allow-list are both configured; True: required
    # (startup refuses and the System page warns while something is missing); False: off
    "ENABLED": None,
    "TOKEN_ENV": "GOZAR_TELEGRAM_BOT_TOKEN",  # name of the environment variable
    # numeric user or chat ids, a list or a comma/space separated string; None: read
    # the environment variable GOZAR_TELEGRAM_ALLOWED_CHAT_IDS
    "ALLOWED_CHAT_IDS": None,
    "INTERACTIVE": False,  # answer /status, /alerts, /ack, /mute, /help (long polling)
    "POLL_IN_WORKER": True,  # gozar_worker polls in a thread when INTERACTIVE is on
    "API_URL": "https://api.telegram.org",
    "TIMEOUT": 10,  # seconds per Bot API call
    "POLL_TIMEOUT": 25,  # seconds a getUpdates long poll waits
    "MAX_MUTE": 7 * 86_400,  # longest /mute
}

ALLOWED_IDS_ENV = "GOZAR_TELEGRAM_ALLOWED_CHAT_IDS"


def configured() -> dict[str, Any]:
    return dict((getattr(settings, "GOZAR", None) or {}).get("ALERTS") or {})


def alert_setting(name: str) -> Any:
    return configured().get(name, DEFAULTS.get(name))


def telegram_configured() -> dict[str, Any]:
    return dict(configured().get("TELEGRAM") or {})


def telegram_setting(name: str) -> Any:
    config = telegram_configured()
    if name == "ALLOWED_CHAT_IDS" and config.get(name) is None:
        return os.environ.get(ALLOWED_IDS_ENV, "")
    return config.get(name, TELEGRAM_DEFAULTS.get(name))


def telegram_token() -> str:
    """The bot token from the environment ('' when unset). Never log or return it."""
    return os.environ.get(str(telegram_setting("TOKEN_ENV") or ""), "").strip()
