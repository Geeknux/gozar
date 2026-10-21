"""Raising and resolving alerts: deduplication, rate limiting, quiet hours, mutes.

``raise_alert(source, key, severity, title, …)``:

1. The source must be on and alerts enabled; the alert key is ``<source>:<key>``.
2. Within the source's window after an alert of the same key was recorded, a
   repeat (no higher severity) only bumps that row's ``repeats`` and the key's
   held-back counter. After the window the next one is a new row carrying the
   count (``suppressed_before``), which its message mentions.
3. A new row is delivered unless it is below the source's minimum severity, its
   key is muted, or no channel is configured. In quiet hours anything below
   critical waits in the outbox until they end.
4. Delivering only inserts ``AlertDelivery`` rows; ``gozar_worker`` sends them.

``resolve(source, key)`` ends a firing condition with a resolved notice, which is
delivered when the condition's alert was.

Neither ever raises: alerting must not break the code that reports a problem.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import zoneinfo
from typing import Any

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, transaction
from django.db.models import F
from django.utils import timezone, translation

from bi_admin.alerts import scrub, sources
from bi_admin.alerts.conf import alert_setting
from bi_admin.alerts.release import release
from bi_admin.models import Alert, AlertDelivery, AlertState

logger = logging.getLogger("bi_admin.alerts")
Delivery = Alert.Delivery
_local = threading.local()


def language() -> str:
    lang = str(alert_setting("LANGUAGE") or "en")
    return lang if lang in ("en", "fa") else "en"


class _Guard:
    """Re-entrancy guard: an alert raised while recording one (a logging handler
    seeing our own database error, say) is dropped."""

    def __enter__(self) -> bool:
        if getattr(_local, "busy", False):
            return False
        _local.busy = True
        return True

    def __exit__(self, *exc) -> None:
        _local.busy = False


def busy() -> bool:
    return bool(getattr(_local, "busy", False))


# --- quiet hours ----------------------------------------------------------------------


def _parse_time(text: str) -> dt.time:
    hours, minutes = text.strip().split(":")
    return dt.time(int(hours), int(minutes))


def quiet_hours() -> tuple[dt.time, dt.time, zoneinfo.ZoneInfo] | None:
    raw = alert_setting("QUIET_HOURS")
    if not raw:
        return None
    try:
        if isinstance(raw, str):
            start, end = (_parse_time(p) for p in raw.split("-"))
        else:
            start, end = (_parse_time(str(p)) for p in raw)
        zone = zoneinfo.ZoneInfo(alert_setting("QUIET_HOURS_TIMEZONE") or settings.TIME_ZONE)
    except (ValueError, TypeError, zoneinfo.ZoneInfoNotFoundError):
        logger.warning("Ignoring GOZAR['ALERTS']['QUIET_HOURS'] = %r: use 'HH:MM-HH:MM'", raw)
        return None
    return (start, end, zone) if start != end else None


def quiet_until(now: dt.datetime) -> dt.datetime | None:
    """The end of the quiet hours ``now`` falls in, or ``None`` outside them."""
    quiet = quiet_hours()
    if quiet is None:
        return None
    start, end, zone = quiet
    local = now.astimezone(zone)
    t = local.time()
    inside = start <= t < end if start < end else (t >= start or t < end)
    if not inside:
        return None
    day = local.date() if t < end else local.date() + dt.timedelta(days=1)
    return dt.datetime.combine(day, end, tzinfo=zone).astimezone(dt.UTC)


# --- channels -------------------------------------------------------------------------


def channels() -> list[tuple[str, list[str]]]:
    """``[(channel, recipients)]`` of every enabled channel."""
    from bi_admin.alerts import telegram

    cfg = telegram.config()
    return [(telegram.NAME, [str(i) for i in cfg.allowed])] if cfg.enabled else []


def _queue(alert: Alert, targets: list[tuple[str, list[str]]], when: dt.datetime) -> int:
    rows = [
        AlertDelivery(alert=alert, channel=name, target=target, next_attempt_at=when)
        for name, recipients in targets
        for target in recipients
    ]
    AlertDelivery.objects.using(DEFAULT_DB_ALIAS).bulk_create(rows)
    return len(rows)


# --- raising and resolving ------------------------------------------------------------


def _record(source: str, key: str, severity: str, title, message, details, now, *, kind):
    cfg = sources.effective(source, sources.overrides())
    if not cfg["enabled"]:
        return None
    full_key = f"{source}:{key}"[:200]
    lang = language()
    with translation.override(lang):
        title_text = scrub.text(title, 300)
        message_text = scrub.text(message, 3000)
    details = scrub.data(details or {})
    with transaction.atomic(using=DEFAULT_DB_ALIAS):
        state, _created = (
            AlertState.objects.using(DEFAULT_DB_ALIAS)
            .select_for_update()
            .get_or_create(key=full_key, defaults={"source": source})
        )
        last = state.last_alert
        muted = state.muted_until is not None and state.muted_until > now
        if kind == Alert.Kind.RESOLVED:
            if not state.firing:
                return None
            severity = state.severity or severity
            title_text = title_text or (last.title if last else full_key)
            deliver = state.notified
        else:
            window = cfg["window"]
            repeat = (
                state.firing
                and last is not None
                and last.kind == Alert.Kind.ALERT
                and window > 0
                and (now - last.created_at).total_seconds() < window
                and sources.rank(severity) <= sources.rank(last.severity)
            )
            if repeat:
                Alert.objects.using(DEFAULT_DB_ALIAS).filter(pk=last.pk).update(
                    repeats=F("repeats") + 1, last_seen_at=now
                )
                AlertState.objects.using(DEFAULT_DB_ALIAS).filter(pk=state.pk).update(
                    suppressed=F("suppressed") + 1
                )
                last.refresh_from_db()
                return last
            deliver = sources.rank(severity) >= sources.rank(cfg["min_severity"])
        targets = channels()
        later = quiet_until(now) if severity != Alert.Severity.CRITICAL else None
        if not deliver:
            status = Delivery.BELOW_THRESHOLD
        elif muted:
            status = Delivery.MUTED
        elif not targets:
            status = Delivery.NO_CHANNEL
        else:
            status = Delivery.DEFERRED if later else Delivery.PENDING
        alert = Alert.objects.using(DEFAULT_DB_ALIAS).create(
            source=source,
            key=full_key,
            severity=severity,
            kind=kind,
            title=title_text,
            message=message_text,
            details=details,
            language=lang,
            release=release(),
            suppressed_before=state.suppressed,
            delivery=status,
            created_at=now,
            last_seen_at=now,
        )
        if status in (Delivery.PENDING, Delivery.DEFERRED):
            _queue(alert, targets, later or now)
        sent = status in (Delivery.PENDING, Delivery.DEFERRED)
        firing = kind == Alert.Kind.ALERT
        AlertState.objects.using(DEFAULT_DB_ALIAS).filter(pk=state.pk).update(
            source=source,
            firing=firing,
            severity=severity if firing else state.severity,
            last_alert=alert,
            suppressed=0,
            last_sent_at=now if sent else state.last_sent_at,
            # a resolved notice ends the episode; a new one starts with this alert
            notified=(firing and (sent or (state.firing and state.notified))),
        )
    return alert


def raise_alert(
    source: str,
    key: str,
    severity: str,
    title,
    message: Any = "",
    details: dict[str, Any] | None = None,
    *,
    now: dt.datetime | None = None,
) -> Alert | None:
    """Record (and queue for delivery) an alert; ``None`` when dropped. Never raises."""
    if severity not in sources.SEVERITIES:
        severity = Alert.Severity.ERROR
    if not alert_setting("ENABLED"):
        return None
    with _Guard() as entered:
        if not entered:
            return None
        try:
            return _record(
                source,
                key,
                severity,
                title,
                message,
                details,
                now or timezone.now(),
                kind=Alert.Kind.ALERT,
            )
        except Exception:
            logger.warning("Could not record the alert %s:%s", source, key, exc_info=True)
            return None


def resolve(
    source: str,
    key: str,
    title: Any = "",
    message: Any = "",
    details: dict[str, Any] | None = None,
    *,
    now: dt.datetime | None = None,
) -> Alert | None:
    """End the firing condition ``key`` with a resolved notice. Never raises."""
    if not alert_setting("ENABLED"):
        return None
    with _Guard() as entered:
        if not entered:
            return None
        try:
            if not (
                AlertState.objects.using(DEFAULT_DB_ALIAS)
                .filter(key=f"{source}:{key}"[:200], firing=True)
                .exists()
            ):
                return None  # the common case: nothing was wrong, no transaction needed
            return _record(
                source,
                key,
                Alert.Severity.INFO,
                title,
                message,
                details,
                now or timezone.now(),
                kind=Alert.Kind.RESOLVED,
            )
        except Exception:
            logger.warning("Could not resolve the alert %s:%s", source, key, exc_info=True)
            return None


def firing(source: str, key: str) -> bool:
    try:
        return (
            AlertState.objects.using(DEFAULT_DB_ALIAS)
            .filter(key=f"{source}:{key}"[:200], firing=True)
            .exists()
        )
    except Exception:
        return False


def firing_keys(source: str) -> list[str]:
    """The firing keys of a source, without the ``<source>:`` prefix."""
    prefix = f"{source}:"
    keys = AlertState.objects.using(DEFAULT_DB_ALIAS).filter(source=source, firing=True)
    return [k[len(prefix) :] for k in keys.values_list("key", flat=True) if k.startswith(prefix)]


def mute(key: str, until: dt.datetime) -> AlertState:
    state, _created = AlertState.objects.using(DEFAULT_DB_ALIAS).get_or_create(
        key=key[:200], defaults={"source": key.split(":", 1)[0]}
    )
    state.muted_until = until
    state.save(update_fields=["muted_until", "updated_at"])
    return state


def acknowledge(alert: Alert, *, user=None, via: str = "web") -> bool:
    return bool(
        Alert.objects.using(DEFAULT_DB_ALIAS)
        .filter(pk=alert.pk, acknowledged_at__isnull=True)
        .update(acknowledged_at=timezone.now(), acknowledged_by=user, acknowledged_via=via)
    )


def purge(now: dt.datetime | None = None) -> int:
    days = float(alert_setting("KEEP_DAYS") or 0)
    if days <= 0:
        return 0
    cutoff = (now or timezone.now()) - dt.timedelta(days=days)
    deleted, _counts = Alert.objects.using(DEFAULT_DB_ALIAS).filter(created_at__lt=cutoff).delete()
    AlertState.objects.using(DEFAULT_DB_ALIAS).filter(
        firing=False, updated_at__lt=cutoff, muted_until__isnull=True
    ).delete()
    return deleted
