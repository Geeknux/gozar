"""E-mail schedules for saved reports and dashboards (owner only).

Recipients are active staff users who can open the report or dashboard; each gets
it with their own scope, language and calendar (see ``bi_admin.scheduling``)."""

import datetime as dt
from typing import Literal

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from ninja import Router, Schema, Status

from bi_admin import scheduling, sharing
from bi_admin.api.saved import Texts, set_texts
from bi_admin.engine import ReportError
from bi_admin.models import Schedule, ScheduleDelivery

router = Router(tags=["schedules"])

Frequency = Literal["daily", "weekly", "monthly"]
Attachment = Literal["", "csv", "xlsx"]
CalendarName = Literal["gregorian", "jalali"]


class ScheduleIn(Schema):
    name: str = ""
    name_i18n: Texts = None
    description: str = ""
    description_i18n: Texts = None
    saved_report: int | None = None
    dashboard: int | None = None
    frequency: Frequency
    time: dt.time
    weekday: int | None = None
    day_of_month: int | None = None
    calendar: CalendarName | None = None
    timezone: str | None = None
    recipients: list[int]
    attachment: Attachment = "xlsx"
    enabled: bool = True


class SchedulePatch(Schema):
    name: str | None = None
    name_i18n: Texts = None
    description: str | None = None
    description_i18n: Texts = None
    frequency: Frequency | None = None
    time: dt.time | None = None
    weekday: int | None = None
    day_of_month: int | None = None
    calendar: CalendarName | None = None
    timezone: str | None = None
    recipients: list[int] | None = None
    attachment: Attachment | None = None
    enabled: bool | None = None


def delivery_payload(d: ScheduleDelivery) -> dict:
    return {
        "id": d.pk,
        "timestamp": d.timestamp.isoformat(),
        "schedule": d.schedule_id,
        "schedule_name": d.schedule_name,
        "user": d.username,
        "email": d.email,
        "status": d.status,
        "status_display": d.get_status_display(),
        "trigger": d.trigger,
        "language": d.language,
        "calendar": d.calendar,
        "reports": d.reports,
        "attachments": d.attachments,
        "error": d.error,
        "duration_ms": d.duration_ms,
    }


def payload(s: Schedule, deliveries: bool = False) -> dict:
    target = s.target
    out = {
        "id": s.pk,
        **sharing.text_payload(s, "name", "description"),
        "target": {
            "kind": "saved_report" if s.saved_report_id else "dashboard",
            "id": target.pk,
            "name": target.text("name"),
        },
        "frequency": s.frequency,
        "frequency_display": s.get_frequency_display(),
        "time": s.time.strftime("%H:%M"),
        "weekday": s.weekday,
        "day_of_month": s.day_of_month,
        "calendar": s.calendar,
        "timezone": s.timezone,
        "recipients": [sharing.person(u) for u in s.recipients.order_by("username")],
        "attachment": s.attachment,
        "enabled": s.enabled,
        "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
    }
    if deliveries:
        out["deliveries"] = [delivery_payload(d) for d in s.deliveries.all()[:20]]
    return out


def _mine(request, schedule_id: int) -> Schedule:
    s = Schedule.objects.filter(pk=schedule_id, owner=request.user).first()
    if s is None:
        raise ReportError(404, "not_found", _("Schedule not found"))
    return s


def _check(s: Schedule, recipients: list[int] | None, request) -> list:
    """Validate timing fields and recipients; returns the recipient users."""
    errors: dict[str, list[str]] = {}
    if s.frequency == Schedule.Frequency.WEEKLY and not (
        s.weekday is not None and 0 <= s.weekday <= 6
    ):
        errors["weekday"] = [_("Choose a weekday (Monday = 0 … Sunday = 6)")]
    if s.frequency == Schedule.Frequency.MONTHLY and not (
        s.day_of_month is not None and 1 <= s.day_of_month <= 31
    ):
        errors["day_of_month"] = [_("Choose a day of the month (1–31)")]
    if not scheduling.is_timezone(s.timezone):
        errors["timezone"] = [_("Unknown time zone")]
    users = []
    if recipients is not None:
        users = list(sharing.staff_users().filter(pk__in=recipients))
        if not recipients:
            errors["recipients"] = [_("Add at least one recipient")]
        elif len(users) != len(set(recipients)):
            errors["recipients"] = [_("Recipients must be active users of the system")]
        else:
            blocked = [u.get_username() for u in users if not sharing.accessible_by(s.target, u)]
            if blocked:
                errors["recipients"] = [
                    _("Not shared with: %(users)s") % {"users": ", ".join(sorted(blocked))}
                ]
            missing = [u.get_username() for u in users if not u.email]
            if missing and "recipients" not in errors:
                errors["recipients"] = [
                    _("No e-mail address: %(users)s") % {"users": ", ".join(sorted(missing))}
                ]
    if errors:
        raise sharing.SharingError(errors)
    return users


@router.get("")
def list_schedules(request):
    """The current user's schedules."""
    qs = Schedule.objects.filter(owner=request.user).select_related("saved_report", "dashboard")
    return {"schedules": [payload(s) for s in qs.prefetch_related("recipients")]}


@router.post("", response={201: dict})
def create_schedule(request, data: ScheduleIn):
    from bi_admin import preferences

    if (data.saved_report is None) == (data.dashboard is None):
        raise sharing.SharingError({"target": [_("Choose a saved report or a dashboard")]})
    s = Schedule(
        owner=request.user,
        frequency=data.frequency,
        time=data.time.replace(second=0, microsecond=0),
        weekday=data.weekday,
        day_of_month=data.day_of_month,
        calendar=data.calendar or preferences.get(request).calendar,
        timezone=data.timezone or scheduling.default_timezone(request.user),
        attachment=data.attachment,
        enabled=data.enabled,
    )
    if data.saved_report is not None:
        s.saved_report = sharing.get_saved_report(request, data.saved_report)
    else:
        s.dashboard = sharing.get_dashboard(request, data.dashboard)
    set_texts(s, data, required=None)
    users = _check(s, data.recipients, request)
    s.next_run_at = scheduling.next_run(s) if s.enabled else None
    with transaction.atomic():
        s.save()
        s.recipients.set(users)
    return Status(201, payload(s, deliveries=True))


@router.get("/{schedule_id}")
def get_schedule(request, schedule_id: int):
    return payload(_mine(request, schedule_id), deliveries=True)


@router.patch("/{schedule_id}")
def update_schedule(request, schedule_id: int, data: SchedulePatch):
    s = _mine(request, schedule_id)
    fields = data.dict(exclude_unset=True)
    recipients = fields.pop("recipients", None)
    for key in ("name", "name_i18n", "description", "description_i18n"):
        fields.pop(key, None)
    set_texts(s, data, required=None)
    for key, value in fields.items():
        if key == "time" and value is not None:
            value = value.replace(second=0, microsecond=0)
        if value is None and key in ("frequency", "time", "calendar", "timezone", "enabled"):
            continue
        setattr(s, key, value)
    users = _check(s, recipients, request)
    s.next_run_at = scheduling.next_run(s) if s.enabled else None
    with transaction.atomic():
        s.save()
        if recipients is not None:
            s.recipients.set(users)
    return payload(s, deliveries=True)


@router.delete("/{schedule_id}")
def delete_schedule(request, schedule_id: int):
    _mine(request, schedule_id).delete()
    return {"deleted": True}


@router.post("/{schedule_id}/send")
def send_now(request, schedule_id: int, only_me: bool = False):
    """Send it now (to every recipient, or with ``only_me`` to the owner as a test);
    the regular timetable is unchanged."""
    s = _mine(request, schedule_id)
    users = [request.user] if only_me else None
    if users and not request.user.email:
        raise sharing.SharingError({"recipients": [_("Your account has no e-mail address")]})
    deliveries = scheduling.deliver(s, trigger="manual", users=users, now=timezone.now())
    return {"deliveries": [delivery_payload(d) for d in deliveries]}
