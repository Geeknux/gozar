"""The System page API, for superusers and users with ``bi_admin.view_system``."""

from django.utils.translation import gettext as _
from ninja import Router

from bi_admin import system
from bi_admin.crud.service import CrudError
from bi_admin.system import db

router = Router(tags=["system"])


def _require(request) -> None:
    if not system.can_view(request.user):
        raise CrudError(
            403, _("You don't have access to the system dashboard."), code="permission_denied"
        )


@router.get("/overview")
def overview(request, refresh: bool = False):
    """Server, versions, apps, cache, job queue, users, activity, report usage,
    deliveries, media storage and migrations."""
    _require(request)
    return system.overview(refresh)


@router.get("/database")
def database(request, refresh: bool = False):
    """Every table with its model, estimated rows, sizes and indexes."""
    _require(request)
    return system.database(refresh)


@router.get("/database/count")
def count(request, table: str):
    """Exact ``COUNT(*)`` of one table (it must be in the introspected table list)."""
    _require(request)
    try:
        return db.exact_count(table)
    except db.UnknownTable:
        raise CrudError(
            404, _("Unknown table %(table)r") % {"table": table}, code="unknown_table"
        ) from None


@router.get("/checks")
def checks(request, refresh: bool = False):
    """Django's system checks, the deployment checks included."""
    _require(request)
    return system.system_checks(refresh)
