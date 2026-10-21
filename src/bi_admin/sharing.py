"""Who may see and change saved reports and dashboards.

Ownership and sharing decide whether an object is *listed*; they never widen what
its data shows. A viewer always runs a shared report or dashboard with their own
model permissions, semantic visibility and row-level scope — the spec is the only
thing that is shared.

Rules:

* the owner always sees and edits it (nobody else edits: others may duplicate);
* ``users``: also the listed users and members of the listed groups;
* ``everyone``: every staff user who may report on its data — for a saved report
  its semantic model, for a dashboard at least one of its widgets' models;
* a shared object whose data the viewer may not report on is not listed for them.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models import Q, QuerySet
from django.utils.translation import gettext as _

from bi_admin.engine.errors import ReportError
from bi_admin.models import Dashboard, SavedReport, Shared
from bi_admin.semantic import semantic_site


def reportable_models(request) -> set[str]:
    """Semantic models the user behind ``request`` may report on (memoised)."""
    cached = getattr(request, "_gozar_reportable", None)
    if cached is None:
        cached = {s.name for s in semantic_site.all() if s.has_permission(request)}
        request._gozar_reportable = cached
    return cached


def _shared_with(user) -> Q:
    return (
        Q(owner=user)
        | Q(visibility=Shared.Visibility.EVERYONE)
        | Q(visibility=Shared.Visibility.USERS, shared_users=user)
        | Q(visibility=Shared.Visibility.USERS, shared_groups__in=user.groups.all())
    )


def _visible_ids(model, user) -> QuerySet:
    return model.objects.filter(_shared_with(user)).values("pk")


def saved_reports(request) -> QuerySet:
    """Saved reports listed for the user: theirs, plus shared ones on models they
    may report on."""
    user = request.user
    allowed = reportable_models(request)
    return (
        SavedReport.objects.filter(pk__in=_visible_ids(SavedReport, user))
        .filter(Q(owner=user) | Q(semantic_model__in=allowed))
        .select_related("owner")
    )


def dashboards(request) -> QuerySet:
    """Dashboards listed for the user: theirs, plus shared ones with at least one
    widget on a model they may report on (a saved-report widget counts by its
    report's model)."""
    user = request.user
    allowed = reportable_models(request)
    usable = Dashboard.objects.filter(
        Q(widgets__saved_report__semantic_model__in=allowed)
        | Q(widgets__spec__semantic_model__in=list(allowed))
    ).values("pk")
    return (
        Dashboard.objects.filter(pk__in=_visible_ids(Dashboard, user))
        .filter(Q(owner=user) | Q(pk__in=usable))
        .select_related("owner")
    )


def can_edit(obj: Shared, user) -> bool:
    return obj.owner_id == user.pk


def get_saved_report(request, pk: int) -> SavedReport:
    report = saved_reports(request).filter(pk=pk).first()
    if report is None:  # someone else's private report is indistinguishable from none
        raise ReportError(404, "not_found", _("Saved report not found"))
    return report


def get_dashboard(request, pk: int) -> Dashboard:
    board = dashboards(request).filter(pk=pk).first()
    if board is None:
        raise ReportError(404, "not_found", _("Dashboard not found"))
    return board


def editable(obj: Shared, request) -> Shared:
    if not can_edit(obj, request.user):
        raise ReportError(
            403, "permission_denied", _("Only the owner can change or delete this; duplicate it")
        )
    return obj


def accessible_by(obj: Shared, user) -> bool:
    """Whether ``user`` may open ``obj`` (used for e-mail recipients)."""
    from bi_admin.execution.jobs import JobRequest

    request = JobRequest(user, "en", "gregorian")
    query = saved_reports if isinstance(obj, SavedReport) else dashboards
    return query(request).filter(pk=obj.pk).exists()


# --- payloads ------------------------------------------------------------------------


def person(user) -> dict[str, Any] | None:
    if user is None:
        return None
    return {
        "id": user.pk,
        "username": user.get_username(),
        "full_name": user.get_full_name() if hasattr(user, "get_full_name") else "",
    }


def sharing_payload(obj: Shared) -> dict[str, Any]:
    return {
        "visibility": obj.visibility,
        # sorted in Python so a prefetch_related() cache is used
        "users": [person(u) for u in sorted(obj.shared_users.all(), key=lambda u: u.username)],
        "groups": [
            {"id": g.pk, "name": g.name}
            for g in sorted(obj.shared_groups.all(), key=lambda g: g.name)
        ],
    }


def text_payload(obj, *fields: str) -> dict[str, Any]:
    """Each field in the active language (with fallbacks) and, as ``<field>_i18n``,
    every language stored for it (for editing)."""
    from bi_admin import translations

    out: dict[str, Any] = {}
    for field in fields:
        out[field] = translations.text(obj, field)
        out[f"{field}_i18n"] = translations.translations(obj, field)
    return out


def common_payload(obj: Shared, request) -> dict[str, Any]:
    return {
        "id": obj.pk,
        **text_payload(obj, "name", "description"),
        "owner": person(obj.owner),
        "is_owner": obj.owner_id == request.user.pk,
        "can_edit": can_edit(obj, request.user),
        "sharing": sharing_payload(obj),
        "created_at": obj.created_at.isoformat(),
        "updated_at": obj.updated_at.isoformat(),
    }


class SharingError(ReportError):
    def __init__(self, errors: dict[str, list[str]]):
        super().__init__(400, "invalid", _("Please correct the errors."), errors=errors)


def staff_users() -> QuerySet:
    return get_user_model().objects.filter(is_active=True, is_staff=True)


def apply_sharing(obj: Shared, sharing: dict[str, Any] | None) -> None:
    """Set visibility and the shared users/groups (by id) from ``sharing``; only
    active staff users can be shared with. ``obj`` must already be saved."""
    if sharing is None:
        return
    visibility = sharing.get("visibility", obj.visibility)
    if visibility not in Shared.Visibility.values:
        raise SharingError({"visibility": [_("Unknown visibility")]})
    user_ids = [int(u) for u in sharing.get("users") or []]
    group_ids = [int(g) for g in sharing.get("groups") or []]
    users = list(staff_users().filter(pk__in=user_ids))
    groups = list(Group.objects.filter(pk__in=group_ids))
    errors = {}
    if len(users) != len(set(user_ids)):
        errors["users"] = [_("Share only with active staff users")]
    if len(groups) != len(set(group_ids)):
        errors["groups"] = [_("Unknown group")]
    if errors:
        raise SharingError(errors)
    obj.visibility = visibility
    obj.save(update_fields=["visibility", "updated_at"])
    obj.shared_users.set(users if visibility == Shared.Visibility.USERS else [])
    obj.shared_groups.set(groups if visibility == Shared.Visibility.USERS else [])
