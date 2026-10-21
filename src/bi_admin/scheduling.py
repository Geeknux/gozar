"""Scheduled e-mail reports.

A ``Schedule`` sends a saved report or a dashboard daily, weekly or monthly at a
local time in its time zone (monthly: a day of the Gregorian or Jalali month,
clamped to the month's length). Recipients are users of the system; each one
gets their own e-mail, built by running the reports *as them* — their
permissions, semantic visibility, row-level scope, language and calendar — so a
schedule can never show anyone more than they could see in Gozar.

``send_due()`` sends every schedule whose ``next_run_at`` has passed; it is run by
``manage.py gozar_send_schedules`` (cron) and by ``gozar_worker``. A schedule is
claimed with a conditional UPDATE of ``next_run_at``, so two processes never send
it twice. Each e-mail is recorded in ``ScheduleDelivery`` and each report it ran
in ``ReportLog`` (kind ``email``).
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import zoneinfo
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone, translation
from django.utils.text import slugify
from django.utils.translation import gettext as _

from bi_admin import formatting, preferences
from bi_admin.calendar import jalali
from bi_admin.conf import setting
from bi_admin.models import Schedule, ScheduleDelivery

logger = logging.getLogger("bi_admin.schedules")
Frequency = Schedule.Frequency


# --- due times -----------------------------------------------------------------------


def zone(name: str | None) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name or settings.TIME_ZONE)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo(settings.TIME_ZONE)


def is_timezone(name: str) -> bool:
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False
    return bool(name)


def month_day(day: dt.date, calendar: str) -> tuple[int, int]:
    """(day of month, length of the month) in ``calendar``."""
    if calendar == "jalali":
        j = jalali.to_jalali(day)
        return j.day, jalali.month_length(j.year, j.month)
    following = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return day.day, (following - dt.timedelta(days=1)).day


def runs_on(
    day: dt.date, frequency: str, weekday: int | None, day_of_month: int | None, calendar: str
) -> bool:
    if frequency == Frequency.DAILY:
        return True
    if frequency == Frequency.WEEKLY:
        return day.weekday() == (weekday or 0)
    current, length = month_day(day, calendar)
    return current == min(day_of_month or 1, length)


def compute_next(
    frequency: str,
    at: dt.time,
    tz: str,
    after: dt.datetime,
    weekday: int | None = None,
    day_of_month: int | None = None,
    calendar: str = "gregorian",
) -> dt.datetime:
    """The first run strictly after ``after``: the next day matching the frequency
    whose local ``at`` (in ``tz``) is later than ``after``. Returned in UTC."""
    local_zone = zone(tz)
    day = after.astimezone(local_zone).date()
    for _i in range(400):
        if runs_on(day, frequency, weekday, day_of_month, calendar):
            moment = dt.datetime.combine(day, at, tzinfo=local_zone)
            if moment > after:
                return moment.astimezone(dt.UTC)
        day += dt.timedelta(days=1)
    raise ValueError("no run time within 400 days")  # unreachable with valid input


def next_run(schedule: Schedule, after: dt.datetime | None = None) -> dt.datetime:
    return compute_next(
        schedule.frequency,
        schedule.time,
        schedule.timezone,
        after or timezone.now(),
        schedule.weekday,
        schedule.day_of_month,
        schedule.calendar,
    )


def default_timezone(user) -> str:
    """The user's time zone preference (``options.timezone``), else ``TIME_ZONE``."""
    return preferences.timezone_of(preferences.for_user(user).options)


# --- building the e-mail ---------------------------------------------------------------


@dataclass
class Section:
    title: str
    result: dict[str, Any] | None = None
    error: str = ""
    interpretation: str = ""
    note: str = ""
    kpis: list[dict[str, str]] = field(default_factory=list)
    headers: list[dict[str, Any]] = field(default_factory=list)
    rows: list[list[dict[str, str]]] = field(default_factory=list)
    hidden_rows: int = 0


def _items(schedule: Schedule, request) -> list[tuple[str, dict | None, str]]:
    """(title, spec or None, problem) for each report the e-mail contains."""
    from bi_admin import dashboards

    if schedule.saved_report_id:
        report = schedule.saved_report
        return [(report.text("name"), dict(report.spec), "")]
    board = schedule.dashboard
    filters = dashboards.clean_filters(board.filters)
    filters.pop("dimensions", None)
    return [
        (w["title"] or _("Widget"), w["spec"], w["problem"])
        for w in dashboards.effective_widgets(board, filters, request)
    ]


def _change_class(value: Any) -> str:
    if not isinstance(value, int | float) or value == 0:
        return "flat"
    return "up" if value > 0 else "down"


def _section(title: str, spec: dict | None, problem: str, request, lang: str) -> Section:
    from bi_admin.engine import ReportError, SpecValidationError, run
    from bi_admin.nl.interpret import describe

    section = Section(title)
    if spec is None:
        section.error = problem or _("This report is not available.")
        return section
    try:
        result = run(spec, request, kind="email")
    except SpecValidationError as exc:
        section.error = "; ".join(e.message for e in exc.errors)
        return section
    except ReportError as exc:
        section.error = str(exc)
        return section
    data = result.as_dict()
    section.result = data
    section.interpretation = describe(result.report, lang)
    columns = data["columns"]
    dims = [c for c in columns if c["role"] == "dimension"]
    measures = [c for c in columns if c["role"] == "measure"]
    if not dims and data["rows"]:
        row = data["rows"][0]
        for m in measures:
            kpi = {
                "label": m["label"],
                "value": formatting.measure(row.get(m["key"]), m["format"], lang),
            }
            key = m["key"]
            if f"{key}__compare" in row:
                direction = _change_class(row.get(f"{key}__change"))
                kpi.update(
                    compare=formatting.measure(row.get(f"{key}__compare"), m["format"], lang),
                    change=formatting.signed(row.get(f"{key}__change_pct"), "percent", lang),
                    direction=direction,
                    arrow={"up": "▲", "down": "▼"}.get(direction, "■"),
                )
            section.kpis.append(kpi)
        return section
    limit = int(setting("EMAIL_MAX_ROWS"))
    section.headers = [{"label": c["label"], "numeric": c["role"] != "dimension"} for c in columns]
    for row in data["rows"][:limit]:
        cells = []
        for c in columns:
            text = formatting.cell(c, row, lang)
            css = "num" if c["role"] != "dimension" else ""
            if c.get("comparison") in ("change", "change_pct"):
                css += " " + _change_class(row.get(c["key"]))
                text = formatting.signed(row.get(c["key"]), c.get("format"), lang)
            cells.append({"text": text, "class": css.strip()})
        section.rows.append(cells)
    section.hidden_rows = max(0, len(data["rows"]) - limit)
    if data["truncated"]:
        section.note = _("The report was cut off at its row limit.")
    return section


def _link(schedule: Schedule) -> str:
    base = (setting("BASE_URL") or "").rstrip("/")
    if not base:
        return ""
    if schedule.saved_report_id:
        return f"{base}/reports?saved={schedule.saved_report_id}"
    return f"{base}/dashboards/{schedule.dashboard_id}"


def build_message(schedule: Schedule, user, now: dt.datetime | None = None):
    """The e-mail for ``user`` (as that user): ``(message, sections)``."""
    from bi_admin.execution.jobs import JobRequest

    prefs = preferences.for_user(user)
    lang, calendar = prefs.language, prefs.calendar
    request = JobRequest(user, lang, calendar)
    now = now or timezone.now()
    with translation.override(lang):
        sections = [
            _section(title, spec, problem, request, lang)
            for title, spec, problem in _items(schedule, request)
        ]
        target = schedule.target
        local = now.astimezone(zone(schedule.timezone))
        when = formatting.date(local.date(), calendar, lang)
        name = schedule.text("name") or target.text("name")
        subject = _("%(name)s · %(date)s") % {"name": formatting.isolate(name, lang), "date": when}
        context = {
            "lang": lang,
            "dir": "rtl" if lang == "fa" else "ltr",
            "subject": subject,
            "title": target.text("name"),
            "description": target.text("description"),
            "note": schedule.text("description"),
            "schedule": schedule,
            "frequency": schedule.get_frequency_display(),
            "sections": sections,
            "link": _link(schedule),
            "attachment": schedule.attachment,
            "generated": formatting.date(local.date(), calendar, lang)
            + formatting._localise(local.strftime(" %H:%M"), lang),
            "user": user,
        }
        html = render_to_string("bi_admin/email/scheduled_report.html", context)
        text = render_to_string("bi_admin/email/scheduled_report.txt", context)
        message = EmailMultiAlternatives(
            subject=subject,
            body=text,
            from_email=setting("EMAIL_FROM") or settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
        )
        message.attach_alternative(html, "text/html")
        if schedule.attachment:
            from bi_admin.engine.export import export

            used: set[str] = set()
            for section in sections:
                if section.result is None or not section.result["rows"]:
                    continue
                body, content_type, filename = export(
                    section.result, schedule.attachment, section.title
                )
                stem = slugify(section.title, allow_unicode=True) or filename.rsplit(".", 1)[0]
                name, n = f"{stem}.{schedule.attachment}", 2
                while name in used:
                    name, n = f"{stem}-{n}.{schedule.attachment}", n + 1
                used.add(name)
                message.attach(name, body, content_type.split(";")[0])
    return message, sections


# --- sending ---------------------------------------------------------------------------


def deliver(
    schedule: Schedule, *, trigger: str = "schedule", users=None, now=None
) -> list[ScheduleDelivery]:
    """Send ``schedule`` to each recipient (or ``users``) now; one delivery row each."""
    from bi_admin import sharing

    out = []
    recipients = list(users if users is not None else schedule.recipients.all())
    target = schedule.target
    for user in recipients:
        prefs = preferences.for_user(user)
        delivery = ScheduleDelivery(
            schedule=schedule,
            schedule_name=(schedule.name or str(target))[:200],
            recipient=user,
            username=user.get_username(),
            email=user.email or "",
            trigger=trigger,
            language=prefs.language,
            calendar=prefs.calendar,
        )
        started = time.monotonic()
        if not (user.is_active and user.is_staff):
            delivery.status, delivery.error = "skipped", "inactive_user"
        elif not user.email:
            delivery.status, delivery.error = "skipped", "no_email_address"
        elif not sharing.accessible_by(target, user):
            delivery.status, delivery.error = "skipped", "not_shared"
        else:
            try:
                message, sections = build_message(schedule, user, now)
                delivery.reports = sum(1 for s in sections if s.result is not None)
                delivery.attachments = len(message.attachments)
                message.send()
                delivery.status = "sent"
            except Exception as exc:  # one bad recipient must not stop the others
                logger.exception("Scheduled e-mail %s to %s failed", schedule.pk, user)
                delivery.status, delivery.error = "failed", f"{type(exc).__name__}: {exc}"[:2000]
        delivery.duration_ms = int((time.monotonic() - started) * 1000)
        delivery.save()
        out.append(delivery)
    return out


def claim(schedule: Schedule, now: dt.datetime) -> bool:
    """Move a due schedule's ``next_run_at`` on; ``False`` if another process did."""
    return bool(
        Schedule.objects.filter(pk=schedule.pk, next_run_at=schedule.next_run_at).update(
            next_run_at=next_run(schedule, now), last_run_at=now
        )
    )


def send_due(now: dt.datetime | None = None) -> list[ScheduleDelivery]:
    """Send every enabled schedule whose ``next_run_at`` is at or before ``now``.
    A schedule missed for a while is sent once, then moves to its next future run."""
    now = now or timezone.now()
    sent = []
    due = Schedule.objects.filter(enabled=True, next_run_at__lte=now).select_related(
        "saved_report", "dashboard"
    )
    for schedule in due:
        if claim(schedule, now):
            sent.extend(deliver(schedule, now=now))
    return sent
