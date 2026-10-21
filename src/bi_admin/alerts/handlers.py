"""``AlertHandler``: a logging handler that turns unhandled exceptions and HTTP 5xx
responses (Django's ``django.request`` logger) into ``exceptions`` alerts.

With ``GOZAR["ALERTS"]["LOG_HANDLER"]`` (default on) Gozar attaches it to
``django.request`` at startup; it can also be named in ``LOGGING``::

    "handlers": {"gozar_alerts": {"class": "bi_admin.alerts.handlers.AlertHandler"}},
    "loggers": {"django.request": {"handlers": ["gozar_alerts"], "level": "ERROR"}},

An alert has the request's method and path (no query string), the user's id
(never their data), the exception type and message, the innermost traceback
frames (file, line, function and the source line — no local variables) and the
release, all scrubbed of secrets. Its key is the exception type plus the
innermost frame inside the project, so every occurrence of one bug is one key.
Emitting never raises and never writes to stderr.
"""

from __future__ import annotations

import hashlib
import logging
import os
import traceback
from typing import Any

from bi_admin.alerts import scrub

IGNORED_LOGGERS = ("bi_admin.alerts", "django.db.backends")


def _user_id(request) -> Any:
    """The user's id when the auth middleware already loaded the user (no query)."""
    from django.utils.functional import empty

    user = getattr(request, "_cached_user", None)
    if user is None:
        candidate = getattr(request, "user", None)
        user = getattr(candidate, "_wrapped", candidate)  # an unevaluated lazy user: empty
    try:
        return user.pk if user is not None and user is not empty and user.is_authenticated else None
    except Exception:
        return None


def _route(request) -> str:
    match = getattr(request, "resolver_match", None)
    route = getattr(match, "route", None) if match is not None else None
    return route or ""


def _frames(tb, limit: int) -> list[traceback.FrameSummary]:
    return traceback.extract_tb(tb)[-limit:] if tb is not None else []


def _short(path: str) -> str:
    """A file path relative to its package root (no home directories in alerts)."""
    parts = path.replace("\\", "/").split("/")
    for marker in ("site-packages", "src", "example_project", "lib"):
        if marker in parts:
            return "/".join(parts[len(parts) - parts[::-1].index(marker) :])
    return "/".join(parts[-3:])


def traceback_tail(exc: BaseException, limit: int) -> str:
    lines = []
    for frame in _frames(exc.__traceback__, limit):
        lines.append(f'File "{_short(frame.filename)}", line {frame.lineno}, in {frame.name}')
        if frame.line:
            lines.append(f"    {frame.line.strip()[:200]}")
    lines.append(f"{type(exc).__name__}: {str(exc)[:300]}")
    return scrub.text("\n".join(lines), 1800)


def exception_key(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    own = [f for f in frames if "site-packages" not in f.filename] or frames
    where = f"{_short(own[-1].filename)}:{own[-1].lineno}" if own else ""
    digest = hashlib.sha256(f"{type(exc).__qualname__}|{where}".encode()).hexdigest()[:16]
    return f"{type(exc).__name__}:{digest}"


class AlertHandler(logging.Handler):
    def __init__(self, level=logging.ERROR):
        super().__init__(level)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith(IGNORED_LOGGERS):
                return
            from bi_admin.alerts import pipeline

            if pipeline.busy():
                return
            self.report(record)
        except Exception:  # alerting never breaks the caller (or prints to stderr)
            logging.getLogger("bi_admin.alerts").debug("AlertHandler failed", exc_info=True)

    def report(self, record: logging.LogRecord) -> None:
        from django.utils.translation import gettext as _

        from bi_admin.alerts import pipeline
        from bi_admin.alerts.conf import alert_setting

        request = getattr(record, "request", None)
        status = getattr(record, "status_code", None)
        exc = record.exc_info[1] if record.exc_info else None
        if exc is None and (status is None or int(status) < 500):
            return
        details: dict[str, Any] = {"logger": record.name, "pid": os.getpid()}
        method = path = ""
        if request is not None and hasattr(request, "method"):
            method = str(getattr(request, "method", "") or "")
            path = str(getattr(request, "path", "") or "")
            details.update(method=method, path=path, route=_route(request))
            details["user_id"] = _user_id(request)
        if status is not None:
            details["status"] = int(status)
        where = f"{method} {path}".strip()
        if exc is not None:
            limit = int(alert_setting("TRACEBACK_FRAMES") or 4)
            details["exception"] = type(exc).__name__
            details["exception_message"] = scrub.text(str(exc), 500)
            details["traceback"] = traceback_tail(exc, limit)
            key = exception_key(exc)
            title = _("Unhandled %(exception)s") % {"exception": type(exc).__name__}
            message = scrub.text(str(exc), 500)
        else:
            route = details.get("route") or path
            key = "http{}:{}".format(
                status, hashlib.sha256(f"{method} {route}".encode()).hexdigest()[:16]
            )
            title = _("HTTP %(status)s response") % {"status": status}
            message = scrub.text(record.getMessage(), 500)
        if where:
            message = f"{where}\n{message}" if message else where
        if details.get("user_id") is not None:
            message += "\n" + _("User id: %(id)s") % {"id": details["user_id"]}
        pipeline.raise_alert("exceptions", key, "error", title, message, details)


def attach() -> None:
    """Add an ``AlertHandler`` to ``django.request`` unless one is there already."""
    target = logging.getLogger("django.request")
    if not any(isinstance(h, AlertHandler) for h in target.handlers):
        target.addHandler(AlertHandler())
