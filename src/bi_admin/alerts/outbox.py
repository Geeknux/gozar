"""The alert outbox: ``AlertDelivery`` rows sent by ``gozar_worker`` (or
``manage.py gozar_alerts --deliver``), never inside a request.

Each due row is claimed with a conditional ``UPDATE`` (a lease token, so two
workers never send it twice), rendered and sent. A transient failure (network,
HTTP 5xx, 429 with ``retry_after``) is retried with exponential backoff
(``RETRY_BACKOFF`` doubling up to ``RETRY_BACKOFF_MAX``) until ``MAX_ATTEMPTS``;
a permanent one (bad request, blocked bot, an id outside the allow-list) fails
at once. A row left in ``sending`` by a dead worker is retried after
``DELIVERY_LEASE_SECONDS``. Failures are logged (without the token) and never
raised.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from django.db import DEFAULT_DB_ALIAS
from django.db.models import Count, F
from django.utils import timezone

from bi_admin.alerts import messages, telegram
from bi_admin.alerts.conf import alert_setting
from bi_admin.models import Alert, AlertDelivery

logger = logging.getLogger("bi_admin.alerts.delivery")
Status = AlertDelivery.Status
BATCH = 50


def backoff(attempts: int) -> float:
    base = float(alert_setting("RETRY_BACKOFF") or 30)
    cap = float(alert_setting("RETRY_BACKOFF_MAX") or 3600)
    return min(cap, base * 2 ** max(0, attempts - 1))


def _rows():
    return AlertDelivery.objects.using(DEFAULT_DB_ALIAS)


def recover(now: dt.datetime) -> int:
    lease = float(alert_setting("DELIVERY_LEASE_SECONDS") or 120)
    return (
        _rows()
        .filter(status=Status.SENDING, leased_at__lt=now - dt.timedelta(seconds=lease))
        .update(status=Status.QUEUED, lease="", next_attempt_at=now)
    )


def claim(pk: int, now: dt.datetime) -> AlertDelivery | None:
    token = uuid.uuid4().hex
    claimed = (
        _rows()
        .filter(pk=pk, status=Status.QUEUED)
        .update(status=Status.SENDING, lease=token, leased_at=now, attempts=F("attempts") + 1)
    )
    if not claimed:
        return None
    return _rows().select_related("alert").get(pk=pk)


def update_alert(alert_id: int, now: dt.datetime) -> None:
    """The alert's delivery status from its rows: sent when all were, failed when
    one gave up, otherwise still queued/held."""
    counts = dict(_rows().filter(alert_id=alert_id).values_list("status").annotate(n=Count("pk")))
    alerts = Alert.objects.using(DEFAULT_DB_ALIAS).filter(pk=alert_id)
    if not counts:
        return
    if counts.get(Status.FAILED):
        alerts.update(delivery=Alert.Delivery.FAILED)
    elif set(counts) == {Status.SENT}:
        alerts.update(delivery=Alert.Delivery.SENT, delivered_at=now)
    elif not counts.get(Status.SENT):
        return
    else:  # partly sent: no longer held for quiet hours
        alerts.filter(delivery=Alert.Delivery.DEFERRED).update(delivery=Alert.Delivery.PENDING)


def _finish(
    row: AlertDelivery, now: dt.datetime, error: str | None, *, permanent=False, retry_after=None
) -> str:
    mine = _rows().filter(pk=row.pk, lease=row.lease, status=Status.SENDING)
    if error is None:
        mine.update(status=Status.SENT, sent_at=now, lease="", last_error="")
        return Status.SENT
    give_up = permanent or row.attempts >= int(alert_setting("MAX_ATTEMPTS") or 1)
    if give_up:
        mine.update(status=Status.FAILED, lease="", last_error=error[:2000])
        logger.error("Alert %s to %s gave up: %s", row.alert_id, row.channel, error)
        return Status.FAILED
    delay = max(float(retry_after or 0), backoff(row.attempts))
    mine.update(
        status=Status.QUEUED,
        lease="",
        last_error=error[:2000],
        next_attempt_at=now + dt.timedelta(seconds=delay),
    )
    logger.warning(
        "Alert %s to %s failed (attempt %s), retrying in %ss: %s",
        row.alert_id,
        row.channel,
        row.attempts,
        int(delay),
        error,
    )
    return Status.QUEUED


def send(row: AlertDelivery, client: telegram.Client | None) -> None:
    if row.channel != telegram.NAME:
        raise telegram.TelegramError(f"unknown channel {row.channel!r}", permanent=True)
    if client is None or not client.cfg.enabled:
        raise telegram.TelegramError("the Telegram channel isn't configured", permanent=True)
    client.send(row.target, messages.render(row.alert))


def deliver_due(now: dt.datetime | None = None, limit: int = BATCH) -> dict[str, int]:
    """Send every due message; ``{"sent", "retry", "failed"}`` counts. Never raises."""
    counts = {"sent": 0, "retry": 0, "failed": 0}
    try:
        now = now or timezone.now()
        recover(now)
        due = list(
            _rows()
            .filter(status=Status.QUEUED, next_attempt_at__lte=now)
            .order_by("next_attempt_at", "id")
            .values_list("pk", flat=True)[:limit]
        )
        if not due:
            return counts
        cfg = telegram.config()
        client = telegram.Client(cfg) if cfg.enabled else None
    except Exception:
        logger.warning("Could not read the alert outbox", exc_info=True)
        return counts
    for pk in due:
        try:
            row = claim(pk, now)
            if row is None:
                continue
            try:
                send(row, client)
                outcome = _finish(row, now, None)
            except telegram.TelegramError as exc:
                outcome = _finish(
                    row, now, str(exc), permanent=exc.permanent, retry_after=exc.retry_after
                )
            except Exception as exc:  # rendering or anything unexpected: retry later
                outcome = _finish(row, now, f"{type(exc).__name__}: {exc}")
            counts[{"sent": "sent", "queued": "retry", "failed": "failed"}[outcome]] += 1
            update_alert(row.alert_id, now)
        except Exception:
            logger.warning("Delivering alert message %s failed", pk, exc_info=True)
    return counts


def stats() -> dict[str, Any]:
    counts = dict(_rows().values_list("status").annotate(n=Count("pk")).order_by())
    oldest = _rows().filter(status=Status.QUEUED).order_by("next_attempt_at").first()
    last_sent = _rows().filter(status=Status.SENT).order_by("-sent_at").first()
    last_failed = _rows().filter(status=Status.FAILED).order_by("-id").first()
    return {
        "queued": counts.get(Status.QUEUED, 0) + counts.get(Status.SENDING, 0),
        "sent": counts.get(Status.SENT, 0),
        "failed": counts.get(Status.FAILED, 0),
        "next_attempt_at": oldest.next_attempt_at.isoformat() if oldest else None,
        "last_sent_at": last_sent.sent_at.isoformat() if last_sent and last_sent.sent_at else None,
        "last_error": last_failed.last_error[:300] if last_failed else None,
    }
