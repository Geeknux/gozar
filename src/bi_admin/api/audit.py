"""The report audit log, for users with ``bi_admin.view_reportlog``."""

from django.core.paginator import EmptyPage, Paginator
from django.db.models import Q
from django.utils.translation import gettext as _
from ninja import Router

from bi_admin.crud.service import CrudError
from bi_admin.models import ReportLog

router = Router(tags=["audit"])

PERMISSION = "bi_admin.view_reportlog"
MAX_PAGE_SIZE = 200


def can_view(user) -> bool:
    return bool(user.is_active and user.is_staff and user.has_perm(PERMISSION))


def entry(log: ReportLog) -> dict:
    return {
        "id": log.pk,
        "timestamp": log.timestamp.isoformat(),
        "user": log.username or None,
        "kind": log.kind,
        "kind_display": log.get_kind_display(),
        "semantic_model": log.semantic_model,
        "spec": log.spec,
        "scope": log.scope,
        "status": log.status,
        "status_display": log.get_status_display(),
        "error_code": log.error_code,
        "error": log.error,
        "duration_ms": log.duration_ms,
        "row_count": log.row_count,
        "cache_hit": log.cache_hit,
        "database": log.database,
        "job": str(log.job_id) if log.job_id else None,
    }


@router.get("/reports")
def report_log(
    request,
    page: int = 1,
    page_size: int = 50,
    user: str = "",
    status: str = "",
    kind: str = "",
    semantic_model: str = "",
    error_code: str = "",
    q: str = "",
):
    """Report executions, newest first; every parameter narrows the list (``q``
    searches users, models, error codes and messages)."""
    if not can_view(request.user):
        raise CrudError(403, _("Permission denied: view the report audit log"))
    qs = ReportLog.objects.all()
    for name, value in {
        "username": user,
        "status": status,
        "kind": kind,
        "semantic_model": semantic_model,
        "error_code": error_code,
    }.items():
        if value:
            qs = qs.filter(**{name: value})
    for term in q.split():
        qs = qs.filter(
            Q(username__icontains=term)
            | Q(semantic_model__icontains=term)
            | Q(error_code__icontains=term)
            | Q(error__icontains=term)
        )
    paginator = Paginator(qs, max(1, min(page_size, MAX_PAGE_SIZE)))
    try:
        current = paginator.page(page)
    except EmptyPage:
        raise CrudError(404, _("Page %(page)s does not exist") % {"page": page}) from None
    return {
        "entries": [entry(log) for log in current.object_list],
        "count": paginator.count,
        "page": current.number,
        "num_pages": paginator.num_pages,
        "filters": {
            "status": [{"value": v, "label": str(label)} for v, label in ReportLog.Status.choices],
            "kind": [{"value": v, "label": str(label)} for v, label in ReportLog.Kind.choices],
            "semantic_model": sorted(
                ReportLog.objects.order_by().values_list("semantic_model", flat=True).distinct()
            ),
        },
    }
