"""Remove secrets from text that goes into an alert.

Applied to every title, message, detail and traceback line before it is stored
or sent: known secret values (the bot token, the LLM API key, ``SECRET_KEY``,
database passwords) are replaced wherever they appear; ``key=value`` /
``"key": value`` pairs whose key names a secret (password, token, cookie, CSRF,
session, authorization, API key…) lose their value; and values that look like a
credential — bearer/basic credentials, Telegram bot tokens, JWTs, ``sk-…`` keys,
AWS access keys, URLs with a password, long hex or base64 strings — are masked.
E-mail addresses are masked too (they are user data).
"""

from __future__ import annotations

import os
import re
from typing import Any

MASK = "[redacted]"

SECRET_NAMES = (
    r"pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|auth(?:orization)?|cookie|set-cookie|csrf(?:token|_token|middlewaretoken)?|"
    r"x-csrftoken|session(?:id|_key)?|sessionid|signature|credential|bearer"
)
SECRET_KEY_RE = re.compile(rf"(?i)^(?:.*[_-])?(?:{SECRET_NAMES})(?:[_-].*)?$")

PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorization: Bearer xyz / Basic xyz / Token xyz
    (re.compile(r"(?i)\b(bearer|basic|token|digest)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {MASK}"),
    # "password": "x", 'token': 'x', password=x, Cookie: a=b; c=d
    (
        re.compile(
            rf"""(?ix)
            (["']?(?:[\w-]*?(?:{SECRET_NAMES})[\w-]*)["']?\s*[:=]\s*)
            (?:"[^"]*"|'[^']*'|[^\s,;&)}}\]]+(?:;\s*[^\s,;&)}}\]]+=[^\s,;&)}}\]]+)*)
            """
        ),
        rf"\1{MASK}",
    ),
    # URLs with credentials: scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@"), rf"\1{MASK}@"),
    # Telegram bot tokens: 123456789:AA…
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}"), MASK),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),
    # OpenAI-style / Slack / GitHub / AWS keys
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), MASK),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), MASK),
    # e-mail addresses
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[email]"),
    # long hex (session keys, hashes, tokens) and long base64-ish runs
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), MASK),
]
# base64-ish runs of 32+ characters that mix digits and both cases (identifiers and
# paths rarely do)
LONG_TOKEN = re.compile(r"(?<![\w/.-])[A-Za-z0-9+/_-]{32,}={0,2}(?![\w/.-])")


def _long_token(match: re.Match[str]) -> str:
    run = match.group(0)
    mixed = any(c.isdigit() for c in run) and any(c.isupper() for c in run)
    return MASK if mixed and any(c.islower() for c in run) else run


def known_secrets() -> list[str]:
    """Secret values configured for this process, longest first."""
    from django.conf import settings

    from bi_admin.alerts.conf import telegram_token

    values = {telegram_token(), getattr(settings, "SECRET_KEY", "") or ""}
    try:
        from bi_admin.nl.conf import api_key

        values.add(api_key())
    except Exception:  # NL not importable in a stripped-down install
        pass
    for conf in getattr(settings, "DATABASES", {}).values():
        values.add(str(conf.get("PASSWORD") or ""))
    for name in ("GOZAR_TELEGRAM_BOT_TOKEN", "GOZAR_NL_API_KEY", "DJANGO_SECRET_KEY"):
        values.add(os.environ.get(name, ""))
    return sorted((v for v in values if len(v) >= 6), key=len, reverse=True)


def text(value: Any, limit: int | None = None) -> str:
    """``value`` as a string with secrets masked, cut to ``limit`` characters."""
    out = "" if value is None else str(value)
    for secret in known_secrets():
        out = out.replace(secret, MASK)
    for pattern, replacement in PATTERNS:
        out = pattern.sub(replacement, out)
    out = LONG_TOKEN.sub(_long_token, out)
    if limit is not None and len(out) > limit:
        out = out[: max(0, limit - 1)] + "…"
    return out


def data(value: Any, depth: int = 0) -> Any:
    """A JSON-ready copy of ``value`` with secret keys dropped and strings masked."""
    if depth > 6:
        return MASK
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k)
            out[key] = MASK if SECRET_KEY_RE.match(key) else data(v, depth + 1)
        return out
    if isinstance(value, list | tuple | set):
        return [data(v, depth + 1) for v in list(value)[:50]]
    if value is None or isinstance(value, bool | int | float):
        return value
    return text(value, 2000)
