"""The Telegram channel: Bot API calls over HTTPS (stdlib ``urllib``), restricted
to an allow-list of numeric user/chat ids.

* The token comes only from the environment variable named by
  ``GOZAR["ALERTS"]["TELEGRAM"]["TOKEN_ENV"]``; it is never stored, logged,
  returned by the API or shown (the System page says whether it is configured).
* ``ALLOWED_CHAT_IDS`` is required. ``send`` refuses any other chat id, whatever
  the caller asks for, and incoming updates are checked against it (``bot``).
* ``config()`` refuses to enable the channel without a token and a valid,
  non-empty allow-list; ``problems`` say why (system check ``bi_admin.W002`` and
  the System page show them).
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from django.utils.translation import gettext as _

from bi_admin.alerts.conf import telegram_configured, telegram_setting, telegram_token

logger = logging.getLogger("bi_admin.alerts.telegram")
NAME = "telegram"
MAX_MESSAGE = 4096  # Bot API limit for sendMessage text
ID_RE = re.compile(r"^-?\d{1,20}$")


class TelegramError(Exception):
    """A Bot API call failed. ``permanent``: retrying can't help (bad request,
    blocked by the user, unknown chat). Messages never contain the token."""

    def __init__(
        self, message: str, *, status: int | None = None, retry_after=None, permanent: bool = False
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.permanent = permanent


class NotAllowed(TelegramError):
    def __init__(self):
        super().__init__("chat id not in the allow-list", permanent=True)


def parse_ids(raw: Any) -> tuple[list[int], list[str]]:
    """``(ids, invalid entries)`` from a list or a comma/space separated string."""
    if raw is None or raw == "":
        return [], []
    items = re.split(r"[\s,;]+", raw.strip()) if isinstance(raw, str) else list(raw)
    ids: list[int] = []
    invalid: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if isinstance(item, bool) or not ID_RE.match(text):
            invalid.append(text)
        elif int(text) not in ids:
            ids.append(int(text))
    return ids, invalid


@dataclass
class Config:
    enabled: bool
    requested: bool | None  # the ENABLED setting
    token_configured: bool
    allowed: list[int] = field(default_factory=list)
    invalid: int = 0
    interactive: bool = False
    problems: list[dict[str, str]] = field(default_factory=list)

    def status(self) -> dict[str, Any]:
        """For the System page and the API: never the token or the ids."""
        return {
            "name": NAME,
            "enabled": self.enabled,
            "requested": self.requested,
            "token_configured": self.token_configured,
            "token_env": str(telegram_setting("TOKEN_ENV") or ""),
            "allowed_count": len(self.allowed),
            "interactive": self.interactive and self.enabled,
            "interactive_requested": self.interactive,
            "problems": self.problems,
        }


def config() -> Config:
    requested = telegram_setting("ENABLED")
    token = telegram_token()
    ids, invalid = parse_ids(telegram_setting("ALLOWED_CHAT_IDS"))
    env = {"env": telegram_setting("TOKEN_ENV")}
    problems: list[dict[str, str]] = []

    def problem(code: str, message: str) -> None:
        problems.append({"code": code, "message": message})

    if telegram_configured().get("TOKEN"):
        message = _("The bot token must not be in settings: export it in %(env)s instead.")
        problem("token_in_settings", message % env)
    if invalid:
        message = _(
            "%(count)s allowed chat id(s) aren't numeric Telegram ids; "
            "the channel stays off until the list is fixed."
        )
        problem("invalid_ids", message % {"count": len(invalid)})
    wanted = requested is True or (requested is None and (bool(token) or bool(ids)))
    if wanted:
        if not token:
            problem("no_token", _("No bot token: set the environment variable %(env)s.") % env)
        if not ids and not invalid:
            message = _(
                "No allowed chat ids: set GOZAR_TELEGRAM_ALLOWED_CHAT_IDS (or "
                'GOZAR["ALERTS"]["TELEGRAM"]["ALLOWED_CHAT_IDS"]). The bot sends only to them.'
            )
            problem("no_allowed_ids", message)
    off = requested is False
    return Config(
        enabled=not off and bool(token) and bool(ids) and not problems,
        requested=requested,
        token_configured=bool(token),
        allowed=ids if not invalid else [],
        invalid=len(invalid),
        interactive=bool(telegram_setting("INTERACTIVE")),
        # switched off, only a token pasted into settings is still worth a word
        problems=[p for p in problems if p["code"] == "token_in_settings"] if off else problems,
    )


def _redact(text: str, token: str) -> str:
    from bi_admin.alerts import scrub

    return scrub.text(text.replace(token, "[token]") if token else text, 500)


def _http(url: str, payload: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
    """POST JSON, return ``(status, body)``; tests replace this function."""
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


class Client:
    """Bot API calls with the configured token. Only ``send`` reaches users, and
    only the allowed ids."""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or config()
        self.token = telegram_token()
        self.base = str(telegram_setting("API_URL") or "https://api.telegram.org").rstrip("/")
        self.timeout = float(telegram_setting("TIMEOUT") or 10)

    def call(self, method: str, params: dict[str, Any] | None = None, timeout=None) -> Any:
        if not self.token:
            raise TelegramError("no bot token configured", permanent=True)
        url = f"{self.base}/bot{self.token}/{method}"
        try:
            status, body = _http(url, params or {}, timeout or self.timeout)
        except Exception as exc:  # network errors, timeouts, bad JSON
            raise TelegramError(_redact(f"{type(exc).__name__}: {exc}", self.token)) from None
        if body.get("ok") and status < 400:
            return body.get("result")
        description = _redact(str(body.get("description") or f"HTTP {status}"), self.token)
        retry_after = (body.get("parameters") or {}).get("retry_after")
        code = int(body.get("error_code") or status or 0)
        raise TelegramError(
            description,
            status=code,
            retry_after=retry_after,
            permanent=code in (400, 401, 403, 404),
        )

    def send(self, chat_id: int | str, text: str) -> Any:
        """sendMessage with HTML parse mode, to an allowed id only."""
        try:
            target = int(str(chat_id))
        except ValueError:
            raise NotAllowed() from None
        if not self.cfg.enabled or target not in self.cfg.allowed:
            raise NotAllowed()
        return self.call(
            "sendMessage",
            {
                "chat_id": target,
                "text": text[:MAX_MESSAGE],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe") or {}

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=timeout + self.timeout) or []
