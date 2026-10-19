"""Saved reports: named ``ReportSpec``s with an owner, a description and sharing.

Anyone may list and run the ones shared with them; the run is always theirs —
their permissions, semantic visibility and row-level scope (the spec goes through
``/reports/run`` like any other). Only the owner changes or deletes one; others
duplicate it."""

from typing import Literal

from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import Q
from django.utils.translation import gettext as _
from ninja import Router, Schema, Status

from bi_admin import sharing
from bi_admin.engine import ReportSpec, SpecValidationError
from bi_admin.engine.runner import prepare
from bi_admin.models import SavedReport

router = Router(tags=["saved reports"])
sharing_router = Router(tags=["sharing"])

Visibility = Literal["private", "users", "everyone"]


class SharingIn(Schema):
    visibility: Visibility = "private"
    users: list[int] = []
    groups: list[int] = []


class SavedReportIn(Schema):
    name: str
    description: str = ""
    spec: ReportSpec
    sharing: SharingIn | None = None


class SavedReportPatch(Schema):
    name: str | None = None
    description: str | None = None
    spec: ReportSpec | None = None
    sharing: SharingIn | None = None


def clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise sharing.SharingError({"name": [_("This field is required.")]})
    if len(name) > 200:
        raise sharing.SharingError({"name": [_("At most 200 characters.")]})
    return name


def stored_spec(spec: ReportSpec, request) -> dict:
    """Validate ``spec`` for the saving user and keep it as submitted (defaults
    dropped; a null calendar stays null so each viewer gets their own)."""
    prepare(spec, request)
    return spec.model_dump(mode="json", exclude_defaults=True)


def payload(report: SavedReport, request, check: bool = True) -> dict:
    out = sharing.common_payload(report, request)
    out.update(semantic_model=report.semantic_model, spec=report.spec)
    if check:  # whether this viewer can run it (a measure may be hidden from them)
        try:
            prepare(ReportSpec.model_validate(report.spec), request)
            out["available"], out["problem"] = True, ""
        except SpecValidationError as exc:
            out["available"], out["problem"] = False, exc.errors[0].message
        except Exception as exc:  # 403 and friends
            out["available"], out["problem"] = False, str(exc)
    return out


@router.get("")
def list_saved(request, q: str = "", mine: bool = False):
    """Saved reports the user owns or that are shared with them, by name."""
    qs = sharing.saved_reports(request).prefetch_related("shared_users", "shared_groups")
    if mine:
        qs = qs.filter(owner=request.user)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
    return {"saved_reports": [payload(r, request) for r in qs.distinct()]}


@router.post("", response={201: dict})
def create_saved(request, data: SavedReportIn):
    with transaction.atomic():
        report = SavedReport.objects.create(
            owner=request.user,
            name=clean_name(data.name),
            description=data.description.strip(),
            semantic_model=data.spec.semantic_model,
            spec=stored_spec(data.spec, request),
        )
        sharing.apply_sharing(report, data.sharing.dict() if data.sharing else None)
    return Status(201, payload(report, request))


@router.get("/{report_id}")
def get_saved(request, report_id: int):
    return payload(sharing.get_saved_report(request, report_id), request)


@router.patch("/{report_id}")
def update_saved(request, report_id: int, data: SavedReportPatch):
    """Rename, describe, change the spec or the sharing (owner only)."""
    report = sharing.editable(sharing.get_saved_report(request, report_id), request)
    with transaction.atomic():
        if data.name is not None:
            report.name = clean_name(data.name)
        if data.description is not None:
            report.description = data.description.strip()
        if data.spec is not None:
            report.spec = stored_spec(data.spec, request)
            report.semantic_model = data.spec.semantic_model
        report.save()
        sharing.apply_sharing(report, data.sharing.dict() if data.sharing else None)
    return payload(report, request)


@router.delete("/{report_id}")
def delete_saved(request, report_id: int):
    """Delete (owner only). Dashboard widgets showing it say so; its e-mail
    schedules are deleted with it."""
    report = sharing.editable(sharing.get_saved_report(request, report_id), request)
    widgets = report.widgets.count()
    report.delete()
    return {"deleted": True, "widgets": widgets}


@router.post("/{report_id}/duplicate", response={201: dict})
def duplicate_saved(request, report_id: int, name: str = ""):
    """A private copy owned by the current user (anyone who can open it)."""
    source = sharing.get_saved_report(request, report_id)
    name = name.strip() or _("Copy of %(name)s") % {"name": source.name}
    copy = SavedReport.objects.create(
        owner=request.user,
        name=clean_name(name[:200]),
        description=source.description,
        semantic_model=source.semantic_model,
        spec=source.spec,
    )
    return Status(201, payload(copy, request))


@sharing_router.get("/targets")
def sharing_targets(request, q: str = ""):
    """Users (active staff) and groups something can be shared with or e-mailed to."""
    users = sharing.staff_users().order_by("username")
    groups = Group.objects.order_by("name")
    if q:
        users = users.filter(
            Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
        )
        groups = groups.filter(name__icontains=q)
    return {
        "users": [sharing.person(u) for u in users[:50]],
        "groups": [{"id": g.pk, "name": g.name} for g in groups[:50]],
    }
