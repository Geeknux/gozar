"""Alert messages for Telegram's HTML parse mode, in the alert language.

Everything that isn't markup goes through ``html.escape``. Persian messages start
each line with a right-to-left mark and wrap inserted values (paths, exception
names, keys) in first-strong isolates, so mixed Latin text keeps its own
direction inside right-to-left lines.
"""

from __future__ import annotations

import copy
import html
from typing import Any

from django.utils import formats, timezone, translation
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from bi_admin.alerts.telegram import MAX_MESSAGE
from bi_admin.models import Alert

ICONS = {"info": "ℹ️", "warning": "⚠️", "error": "❗", "critical": "🚨"}
RESOLVED_ICON = "✅"
RLM, FSI, PDI = "‏", "⁨", "⁩"
PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


class Writer:
    def __init__(self, lang: str):
        self.rtl = lang == "fa"
        self.lines: list[str] = []

    def value(self, text: Any) -> str:
        """An inserted value, isolated in right-to-left messages."""
        out = esc(text)
        return f"{FSI}{out}{PDI}" if self.rtl and out else out

    def number(self, n: int) -> str:
        return str(n).translate(PERSIAN_DIGITS) if self.rtl else str(n)

    def add(self, line: str = "") -> None:
        self.lines.append(f"{RLM}{line}" if self.rtl and line else line)

    def text(self) -> str:
        return "\n".join(self.lines).strip()


def severity_label(severity: str) -> str:
    return str(dict(Alert.Severity.choices).get(severity, severity))


def link(alert: Alert) -> str | None:
    from bi_admin.conf import setting

    base = setting("BASE_URL")
    if not base:
        return None
    return f"{str(base).rstrip('/')}/system?alert={alert.pk}"


def window_text(seconds: int) -> str:
    if seconds % 3600 == 0 and seconds >= 3600:
        hours = seconds // 3600
        return ngettext("%(n)s hour", "%(n)s hours", hours) % {"n": hours}
    minutes = max(1, seconds // 60)
    return ngettext("%(n)s minute", "%(n)s minutes", minutes) % {"n": minutes}


def render(alert: Alert) -> str:
    """The sendMessage text (HTML) of ``alert``."""
    lang = alert.language or "en"
    with translation.override(lang):
        w = Writer(lang)
        if alert.kind == Alert.Kind.RESOLVED:
            head = f"{RESOLVED_ICON} <b>{esc(_('Resolved'))}</b>"
        else:
            icon = ICONS.get(alert.severity, "")
            head = f"{icon} <b>{esc(severity_label(alert.severity).upper())}</b>"
        w.add(f"{head} · <b>{w.value(alert.title)}</b>")
        if alert.message:
            w.add()
            for line in alert.message.splitlines()[:30]:
                w.add(w.value(line) if line.strip() else "")
        tb = (alert.details or {}).get("traceback")
        if tb:
            w.add()
            w.add(f"<pre>{esc(tb)}</pre>")
        if alert.suppressed_before:
            n = alert.suppressed_before
            w.add()
            w.add(
                esc(
                    ngettext(
                        "%(n)s more alert like this was held back since the last message.",
                        "%(n)s more alerts like this were held back since the last message.",
                        n,
                    )
                    % {"n": w.number(n)}
                )
            )
        w.add()
        meta = [f"#{w.number(alert.pk)}", w.value(alert.source)]
        if alert.release:
            meta.append(w.value(alert.release))
        w.add(f"<i>{' · '.join(meta)}</i>")
        when = formats.date_format(timezone.localtime(alert.created_at), "Y-m-d H:i:s")
        w.add(f"<i>{w.value(when)}</i>")
        url = link(alert)
        if url:
            w.add(f'<a href="{html.escape(url, quote=True)}">{esc(_("Open in Gozar"))}</a>')
        text = w.text()
    if len(text) > MAX_MESSAGE:  # drop the traceback first, then cut the message
        shorter = copy.copy(alert)
        shorter.details = {k: v for k, v in (alert.details or {}).items() if k != "traceback"}
        if not tb:
            excess = len(text) - MAX_MESSAGE + 10
            shorter.message = alert.message[: max(0, len(alert.message) - excess)] + "…"
        return render(shorter)
    return text
