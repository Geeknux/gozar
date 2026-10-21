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

from bi_admin import sharing, translations
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


# ``name``/``description`` are the default language's text; ``*_i18n`` map each
# language to its text (see bi_admin.translations) and replace the stored ones.
Texts = dict[str, str | None] | None


class SavedReportIn(Schema):
    name: str = ""
    name_i18n: Texts = None
    description: str = ""
    description_i18n: Texts = None
    spec: ReportSpec
    sharing: SharingIn | None = None


class SavedReportPatch(Schema):
    name: str | None = None
    name_i18n: Texts = None
    description: str | None = None
    description_i18n: Texts = None
    spec: ReportSpec | None = None
    sharing: SharingIn | None = None


def clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise sharing.SharingError({"name": [_("This field is required.")]})
    if len(name) > 200:
        raise sharing.SharingError({"name": [_("At most 200 characters.")]})
    return name


def set_texts(obj, data, fields=("name", "description"), required: str | None = "name") -> None:
    """Apply ``data``'s plain and per-language texts to ``obj`` (unsaved). Fields
    ``data`` leaves out (``None``) are kept."""
    errors: dict[str, list[str]] = {}
    for field in fields:
        plain = getattr(data, field, None)
        values = getattr(data, f"{field}_i18n", None)
        max_length = obj._meta.get_field(field).max_length
        try:
            values = translations.clean(values, f"{field}_i18n", max_length)
            values = values if getattr(data, f"{field}_i18n", None) is not None else None
        except translations.TranslationError as exc:
            errors.update(exc.errors)
            continue
        if plain is not None:
            plain = translations.clean_text(plain)
            if max_length and len(plain) > max_length:
                errors[field] = [_("At most %(n)s characters.") % {"n": max_length}]
                continue
        translations.assign(obj, field, plain, values)
    if required and required not in errors and not getattr(obj, required):
        errors[required] = [_("This field is required.")]
    if errors:
        raise sharing.SharingError(errors)


def copy_texts(source, copy, name: str = "") -> None:
    """Texts of a duplicate: ``name`` when given (the default language's), else
    "Copy of …" in each language; the descriptions as they are."""
    if name.strip():
        translations.assign(copy, "name", clean_name(name[:200]), None)
    else:
        names = translations.copy_of(translations.translations(source, "name"))
        translations.assign(copy, "name", None, names)
    translations.assign(copy, "description", None, translations.translations(source, "description"))


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
        qs = qs.filter(translations.search_q(q, "name", "description"))
    return {"saved_reports": [payload(r, request) for r in qs.distinct()]}


@router.post("", response={201: dict})
def create_saved(request, data: SavedReportIn):
    report = SavedReport(
        owner=request.user,
        semantic_model=data.spec.semantic_model,
        spec=stored_spec(data.spec, request),
    )
    set_texts(report, data)
    with transaction.atomic():
        report.save()
        sharing.apply_sharing(report, data.sharing.dict() if data.sharing else None)
    return Status(201, payload(report, request))


@router.get("/{report_id}")
def get_saved(request, report_id: int):
    return payload(sharing.get_saved_report(request, report_id), request)


@router.patch("/{report_id}")
def update_saved(request, report_id: int, data: SavedReportPatch):
    """Rename, describe, change the spec or the sharing (owner only)."""
    report = sharing.editable(sharing.get_saved_report(request, report_id), request)
    set_texts(report, data)
    with transaction.atomic():
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
    copy = SavedReport(
        owner=request.user,
        semantic_model=source.semantic_model,
        spec=source.spec,
    )
    copy_texts(source, copy, name)
    copy.save()
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
