"""The optional interactive Telegram bot (``GOZAR["ALERTS"]["TELEGRAM"]["INTERACTIVE"]``):
``/status``, ``/alerts``, ``/ack <id>``, ``/mute <key> <duration>``, ``/unmute <key>``
and ``/help``, received by long polling (``getUpdates``) in ``gozar_worker`` or
``manage.py gozar_telegram_poll`` — no webhook.

Every update is checked before anything else: it must be a plain ``message`` in a
**private** chat whose ``chat.id`` equals the sender's ``from.id``, and that id
must be in the allow-list; messages sent on behalf of a chat, through another bot,
by a bot, or forwarded are ignored. Anything from someone outside the allow-list
is ignored silently — no reply — apart from a rate-limited log line and an
alert to the allowed users. Replies only ever go to the sender's own (allowed)
chat and only carry operational status, never report data.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import threading
import time
from typing import Any

from django.db import DEFAULT_DB_ALIAS
from django.db.models import Count
from django.utils import formats, timezone, translation
from django.utils.translation import gettext as _

from bi_admin.alerts import messages, pipeline, telegram
from bi_admin.alerts.conf import telegram_setting
from bi_admin.alerts.messages import esc
from bi_admin.models import Alert, AlertState, ReportJob

logger = logging.getLogger("bi_admin.alerts.telegram")
OFFSET_KEY = "telegram:_offset"
FORWARD_FIELDS = (
    "forward_origin",
    "forward_from",
    "forward_from_chat",
    "forward_sender_name",
    "forward_date",
    "is_automatic_forward",
)
DURATION_RE = re.compile(r"^(\d{1,5})([smhd])$")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86_400}
LOG_EVERY = 60.0  # seconds between log lines about ignored updates
_log_state = {"at": 0.0, "count": 0}
_log_lock = threading.Lock()


def _int_id(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _sender(update: dict[str, Any]) -> tuple[int | None, dict[str, Any], dict[str, Any]]:
    """``(from.id, from, chat)`` of any update type."""
    for name, body in update.items():
        if name == "update_id" or not isinstance(body, dict):
            continue
        sender = body.get("from") if isinstance(body.get("from"), dict) else {}
        chat = body.get("chat") if isinstance(body.get("chat"), dict) else {}
        if not chat and isinstance(body.get("message"), dict):  # callback queries
            chat = body["message"].get("chat") or {}
        return _int_id(sender.get("id")), sender, chat
    return None, {}, {}


def authorise(update: dict[str, Any], allowed: list[int]) -> tuple[str, dict[str, Any] | None]:
    """``("ok", message)``, or a reason the update is ignored."""
    from_id, sender, chat = _sender(update)
    if from_id is None or from_id not in allowed:
        return "unknown_sender", None
    message = update.get("message")
    if not isinstance(message, dict) or set(update) - {"update_id", "message"}:
        return "not_a_message", None
    if sender.get("is_bot"):
        return "bot_sender", None
    if chat.get("type") != "private":
        return "not_private", None
    if _int_id(chat.get("id")) != from_id:
        return "chat_mismatch", None
    if message.get("sender_chat") or message.get("via_bot"):
        return "on_behalf", None
    if any(message.get(k) for k in FORWARD_FIELDS):
        return "forwarded", None
    if not isinstance(message.get("text"), str):
        return "no_text", None
    return "ok", message


def _log_ignored(reason: str) -> None:
    """At most one log line per ``LOG_EVERY`` seconds, with the count meanwhile."""
    with _log_lock:
        _log_state["count"] += 1
        now = time.monotonic()
        if now - _log_state["at"] < LOG_EVERY:
            return
        count, _log_state["count"], _log_state["at"] = _log_state["count"], 0, now
    logger.warning("Ignored %s Telegram update(s) (latest: %s)", count, reason)


def unauthorised(update: dict[str, Any]) -> None:
    from_id, sender, chat = _sender(update)
    _log_ignored("unknown_sender")
    who = f"{from_id}" if from_id is not None else "?"
    if sender.get("username"):
        who += f" (@{str(sender['username'])[:40]})"
    pipeline.raise_alert(
        "telegram",
        f"unauthorised:{from_id}",
        "warning",
        _("Someone outside the allow-list used the Telegram bot"),
        _(
            "Telegram user %(who)s wrote to the bot in a %(type)s chat. They got no reply; "
            "nothing was done."
        )
        % {"who": who, "type": str(chat.get("type") or "?")[:20]},
        {"from_id": from_id, "chat_type": chat.get("type")},
    )


# --- commands -------------------------------------------------------------------------


def _ago(value: dt.datetime | None, now: dt.datetime) -> str:
    if value is None:
        return _("never")
    seconds = max(0, int((now - value).total_seconds()))
    if seconds < 120:
        return _("%(n)s s ago") % {"n": seconds}
    if seconds < 7200:
        return _("%(n)s min ago") % {"n": seconds // 60}
    return _("%(n)s h ago") % {"n": seconds // 3600}


def status_text(now: dt.datetime | None = None) -> str:
    from bi_admin.alerts import monitors, outbox
    from bi_admin.alerts.release import release

    now = now or timezone.now()
    worker = monitors.worker_status(now)
    last = dt.datetime.fromisoformat(worker["last_beat_at"]) if worker["last_beat_at"] else None
    if last is None:
        worker_line = _("Worker: no running worker seen")
    elif worker["stale"]:
        worker_line = _("Worker: ⚠️ stale, last heartbeat %(ago)s") % {"ago": _ago(last, now)}
    else:
        worker_line = _("Worker: running, last heartbeat %(ago)s") % {"ago": _ago(last, now)}
    since = now - dt.timedelta(hours=24)
    jobs = ReportJob.objects.using(DEFAULT_DB_ALIAS)
    counts = dict(jobs.values_list("status").annotate(n=Count("pk")).order_by())
    failed = jobs.filter(status=ReportJob.Status.FAILED, finished_at__gte=since).count()
    recent = Alert.objects.using(DEFAULT_DB_ALIAS).filter(created_at__gte=since)
    by_severity = dict(
        recent.filter(kind=Alert.Kind.ALERT)
        .values_list("severity")
        .annotate(n=Count("pk"))
        .order_by()
    )
    firing = AlertState.objects.using(DEFAULT_DB_ALIAS).filter(firing=True).count()
    lines = [
        f"<b>{esc(_('Gozar status'))}</b>",
        esc(worker_line),
        esc(
            _("Jobs: %(queued)s queued, %(running)s running, %(failed)s failed in 24 h")
            % {
                "queued": counts.get("queued", 0),
                "running": counts.get("running", 0),
                "failed": failed,
            }
        ),
        esc(
            _(
                "Alerts: %(firing)s firing; 24 h: %(critical)s critical, %(error)s errors, "
                "%(warning)s warnings"
            )
            % {
                "firing": firing,
                "critical": by_severity.get("critical", 0),
                "error": by_severity.get("error", 0),
                "warning": by_severity.get("warning", 0),
            }
        ),
        esc(_("Outbox: %(n)s waiting") % {"n": outbox.stats()["queued"]}),
        f"<i>{esc(release())}</i>",
    ]
    return "\n".join(lines)


def alerts_text(limit: int = 10) -> str:
    rows = Alert.objects.using(DEFAULT_DB_ALIAS).order_by("-created_at", "-id")[:limit]
    if not rows:
        return esc(_("No alerts."))
    lines = [f"<b>{esc(_('Latest alerts'))}</b>"]
    for a in rows:
        icon = (
            messages.RESOLVED_ICON
            if a.kind == Alert.Kind.RESOLVED
            else messages.ICONS.get(a.severity, "")
        )
        when = formats.date_format(timezone.localtime(a.created_at), "m-d H:i")
        ack = " ✔︎" if a.acknowledged_at else ""
        repeats = f" ×{a.repeats + 1}" if a.repeats else ""
        lines.append(
            f"#{a.pk} {icon} {esc(a.title[:80])}{repeats}{ack}\n"
            f"   <code>{esc(a.key[:80])}</code> · {esc(when)}"
        )
    return "\n".join(lines)


def help_text() -> str:
    return "\n".join(
        [
            f"<b>{esc(_('Gozar alerts bot'))}</b>",
            esc(_("/status — worker, jobs, alerts and outbox")),
            esc(_("/alerts — the latest alerts")),
            esc(_("/ack <id> — acknowledge an alert")),
            esc(_("/mute <key> <duration> — hold back an alert key (e.g. 30m, 2h, 1d)")),
            esc(_("/unmute <key> — deliver it again")),
            esc(_("/help — this list")),
        ]
    )


def parse_duration(text: str) -> int | None:
    match = DURATION_RE.match(text.strip().lower())
    if not match:
        return None
    return int(match.group(1)) * UNITS[match.group(2)]


def command(text: str, from_id: int, now: dt.datetime | None = None) -> str:
    now = now or timezone.now()
    parts = text.strip().split()
    if not parts:
        return help_text()
    name = parts[0].split("@", 1)[0].lower()
    args = parts[1:]
    if name in ("/start", "/help"):
        return help_text()
    if name == "/status":
        return status_text(now)
    if name == "/alerts":
        return alerts_text()
    if name == "/ack":
        if len(args) != 1 or not args[0].lstrip("#").isdigit():
            return esc(_("Usage: /ack <id>"))
        alert = Alert.objects.using(DEFAULT_DB_ALIAS).filter(pk=int(args[0].lstrip("#"))).first()
        if alert is None:
            return esc(_("No alert #%(id)s.") % {"id": args[0].lstrip("#")})
        if not pipeline.acknowledge(alert, via=f"telegram:{from_id}"):
            return esc(_("Alert #%(id)s was already acknowledged.") % {"id": alert.pk})
        return esc(_("Alert #%(id)s acknowledged.") % {"id": alert.pk})
    if name in ("/mute", "/unmute"):
        if len(args) != (2 if name == "/mute" else 1):
            return esc(_("Usage: /mute <key> <duration>, /unmute <key>"))
        key = args[0][:200]
        if not AlertState.objects.using(DEFAULT_DB_ALIAS).filter(key=key).exists():
            return esc(_("Unknown alert key %(key)s (see /alerts).") % {"key": key})
        if name == "/unmute":
            pipeline.mute(key, now)
            return esc(_("%(key)s is no longer muted.") % {"key": key})
        seconds = parse_duration(args[1])
        limit = int(telegram_setting("MAX_MUTE") or 7 * 86_400)
        if seconds is None or seconds <= 0:
            return esc(_("Durations look like 30m, 2h or 1d."))
        until = now + dt.timedelta(seconds=min(seconds, limit))
        pipeline.mute(key, until)
        shown = formats.date_format(timezone.localtime(until), "Y-m-d H:i")
        return esc(_("%(key)s muted until %(time)s.") % {"key": key, "time": shown})
    return esc(_("Unknown command. Send /help for the list."))


# --- polling --------------------------------------------------------------------------


def handle(update: dict[str, Any], client: telegram.Client, now=None) -> str:
    """Process one update; returns what happened (for tests and logs)."""
    reason, message = authorise(update, client.cfg.allowed)
    if reason == "unknown_sender":
        unauthorised(update)
        return reason
    if message is None:
        _log_ignored(reason)
        return reason
    with translation.override(pipeline.language()):
        reply = command(message["text"], message["from"]["id"], now)
    client.send(message["chat"]["id"], reply)
    return "ok"


def _offset() -> int | None:
    state = AlertState.objects.using(DEFAULT_DB_ALIAS).filter(key=OFFSET_KEY).first()
    value = (state.data or {}).get("offset") if state else None
    return int(value) if value is not None else None


def _save_offset(offset: int) -> None:
    AlertState.objects.using(DEFAULT_DB_ALIAS).update_or_create(
        key=OFFSET_KEY, defaults={"source": "telegram", "data": {"offset": offset}}
    )


def poll_once(client: telegram.Client, timeout: int | None = None) -> int:
    """One getUpdates round; returns the number of updates processed."""
    if timeout is None:
        timeout = int(telegram_setting("POLL_TIMEOUT") or 25)
    updates = client.get_updates(_offset(), timeout)
    for update in updates:
        update_id = update.get("update_id") if isinstance(update, dict) else None
        try:
            handle(update, client)
        except Exception:
            logger.warning("Handling a Telegram update failed", exc_info=True)
        if isinstance(update_id, int):
            _save_offset(update_id + 1)
    return len(updates)


def require_interactive() -> telegram.Client:
    """The client for polling, or ``RuntimeError`` saying why polling can't start."""
    cfg = telegram.config()
    if not cfg.enabled:
        problems = "; ".join(p["message"] for p in cfg.problems) or _("the Telegram channel is off")
        raise RuntimeError(problems)
    if not cfg.interactive:
        raise RuntimeError(
            _('Interactive mode is off (GOZAR["ALERTS"]["TELEGRAM"]["INTERACTIVE"]).')
        )
    return telegram.Client(cfg)


def run(stop: threading.Event | None = None, *, once: bool = False) -> None:
    """Poll until ``stop`` is set; errors back off and never end the loop (with
    ``once``: a single short poll, errors raised)."""
    client = require_interactive()
    delay = 1.0
    while stop is None or not stop.is_set():
        try:
            poll_once(client, 0 if once else None)
            delay = 1.0
        except telegram.TelegramError as exc:
            logger.warning("Telegram getUpdates failed: %s", exc)
            if once:
                raise
            delay = min(300.0, max(delay * 2, float(exc.retry_after or 0)))
            if stop is not None:
                stop.wait(delay)
            else:
                time.sleep(delay)
        except Exception:
            if once:
                raise
            logger.warning("Telegram polling failed", exc_info=True)
            time.sleep(5)
        if once:
            break


def start_thread() -> tuple[threading.Thread, threading.Event] | None:
    """Poll in a daemon thread (``gozar_worker``); ``None`` when interactive mode
    isn't usable."""
    from django.db import connections

    try:
        require_interactive()
    except RuntimeError:
        return None
    stop = threading.Event()

    def target():
        try:
            run(stop)
        except Exception:
            logger.warning("Telegram polling stopped", exc_info=True)
        finally:
            connections.close_all()  # this thread's own connections

    thread = threading.Thread(target=target, name="gozar-telegram-poll", daemon=True)
    thread.start()
    return thread, stop
