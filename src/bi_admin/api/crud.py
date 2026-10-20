from django.http import HttpResponse
from django.utils.translation import gettext as _
from ninja import Router, Status

from bi_admin.crud import bulk, listviews, search, service, transfer, users
from bi_admin.crud.forms import PayloadError, read_payload

router = Router(tags=["crud"])
views_router = Router(tags=["crud"])
search_router = Router(tags=["crud"])

DOWNLOAD_PARAMS = {"format", "pks"}


def _payload(request):
    try:
        return read_payload(request)
    except PayloadError as exc:
        raise service.CrudError(400, str(exc)) from None


def _body(request):
    return _payload(request)[0]


def _opts(app_label: str, model_name: str):
    return service.get_options(f"{app_label}.{model_name}")


def _download(body: bytes, content_type: str, filename: str, truncated=False) -> HttpResponse:
    response = HttpResponse(body, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    if truncated:
        response["X-Gozar-Truncated"] = "true"
    return response


@router.get("/{app_label}/{model_name}/")
def list_objects(request, app_label: str, model_name: str):
    """Query params: q, page, page_size, ordering, columns, the date hierarchy
    (dh_year, dh_month, dh_day, dh_calendar) and filters like ``status=paid``,
    ``created_at__date__gte=2025-01-01``, ``customer__city=3``, ``paid_at__isnull=true``."""
    opts = _opts(app_label, model_name)
    return service.list_objects(opts, request, request.GET.dict())


@router.patch("/{app_label}/{model_name}/")
def update_rows(request, app_label: str, model_name: str):
    """Save edited ``list_editable`` cells: ``{"rows": [{"pk", "values"}]}``."""
    opts = _opts(app_label, model_name)
    return bulk.update_rows(opts, request, _body(request).get("rows"))


@router.post("/{app_label}/{model_name}/", response={201: dict})
def create_object(request, app_label: str, model_name: str):
    opts = _opts(app_label, model_name)
    payload, files = _payload(request)
    obj = service.save_object(opts, request, payload, files=files)
    return Status(201, service.serialize_object(opts, obj, request))


@router.get("/{app_label}/{model_name}/new")
def new_object(request, app_label: str, model_name: str):
    """Default values for an empty create form."""
    opts = _opts(app_label, model_name)
    service.require(opts, request, "add")
    return service.serialize_object(opts, opts.model(), request)


@router.get("/{app_label}/{model_name}/autocomplete")
def autocomplete(
    request,
    app_label: str,
    model_name: str,
    field: str,
    q: str = "",
    inline: str = "",
    limit: int = service.AUTOCOMPLETE_LIMIT,
):
    opts = _opts(app_label, model_name)
    return service.autocomplete(opts, request, field, q, inline or None, limit)


@router.post("/{app_label}/{model_name}/actions/{action}")
def run_action(request, app_label: str, model_name: str, action: str):
    """Body: ``{"pks": [...]}`` or ``{"all": true, "params": {...list query...}}``."""
    opts = _opts(app_label, model_name)
    return service.run_action(opts, request, action, _body(request))


@router.post("/{app_label}/{model_name}/delete-plan")
def bulk_delete_plan(request, app_label: str, model_name: str):
    opts = _opts(app_label, model_name)
    return service.action_plan(opts, request, _body(request))


@router.post("/{app_label}/{model_name}/bulk-edit")
def bulk_edit(request, app_label: str, model_name: str):
    """``{"pks": [...] | "all": true + "params", "values": {field: value}}``."""
    opts = _opts(app_label, model_name)
    return bulk.bulk_edit(opts, request, _body(request))


@router.get("/{app_label}/{model_name}/export")
def export_list(request, app_label: str, model_name: str, format: str = "csv", pks: str = ""):
    """The list (same query params) as CSV or XLSX; ``pks=1,2`` limits it to a selection."""
    opts = _opts(app_label, model_name)
    params = {k: v for k, v in request.GET.dict().items() if k not in DOWNLOAD_PARAMS}
    selected = [p for p in pks.split(",") if p] if pks else None
    return _download(*transfer.export_list(opts, request, params, format, selected))


@router.get("/{app_label}/{model_name}/import-template")
def import_template(request, app_label: str, model_name: str, format: str = "csv"):
    opts = _opts(app_label, model_name)
    return _download(*transfer.template(opts, request, format))


@router.post("/{app_label}/{model_name}/import")
def import_file(request, app_label: str, model_name: str, dry_run: bool = True):
    """Multipart upload ``file`` (CSV or XLSX). ``dry_run=true`` (default) validates
    and reports without saving; ``false`` saves when no row has an error."""
    opts = _opts(app_label, model_name)
    return transfer.import_file(opts, request, request.FILES.get("file"), dry_run)


@router.get("/{app_label}/{model_name}/permission-tree")
def permission_tree(request, app_label: str, model_name: str, field: str):
    opts = _opts(app_label, model_name)
    return users.permission_tree(opts, request, field)


@router.get("/{app_label}/{model_name}/password-form")
def password_form(request, app_label: str, model_name: str):
    opts = _opts(app_label, model_name)
    service.require(opts, request, "change")
    return users.password_form(opts)


@router.get("/{app_label}/{model_name}/{pk}")
def get_object(request, app_label: str, model_name: str, pk: str):
    opts = _opts(app_label, model_name)
    service.require(opts, request, "view")
    return service.serialize_object(opts, service.get_object(opts, request, pk), request)


def _update(request, app_label, model_name, pk, partial):
    opts = _opts(app_label, model_name)
    service.require(opts, request, "change")
    obj = service.get_object(opts, request, pk)
    payload, files = _payload(request)
    obj = service.save_object(opts, request, payload, obj, partial=partial, files=files)
    return service.serialize_object(opts, obj, request)


@router.patch("/{app_label}/{model_name}/{pk}")
def update_object(request, app_label: str, model_name: str, pk: str):
    return _update(request, app_label, model_name, pk, partial=True)


@router.put("/{app_label}/{model_name}/{pk}")
def replace_object(request, app_label: str, model_name: str, pk: str):
    return _update(request, app_label, model_name, pk, partial=False)


@router.get("/{app_label}/{model_name}/{pk}/delete-plan")
def delete_plan(request, app_label: str, model_name: str, pk: str):
    opts = _opts(app_label, model_name)
    service.require(opts, request, "delete")
    obj = service.get_object(opts, request, pk)
    service.require(opts, request, "delete", obj)
    return service.deletion_plan(request, [obj], opts.model)


@router.delete("/{app_label}/{model_name}/{pk}")
def delete_object(request, app_label: str, model_name: str, pk: str):
    opts = _opts(app_label, model_name)
    service.require(opts, request, "delete")
    obj = service.get_object(opts, request, pk)
    return service.delete_objects(opts, request, [obj])


@router.get("/{app_label}/{model_name}/{pk}/history")
def history(request, app_label: str, model_name: str, pk: str):
    opts = _opts(app_label, model_name)
    service.get_object(opts, request, pk)
    return {"entries": service.history(opts, request, pk)}


@router.post("/{app_label}/{model_name}/{pk}/password")
def change_password(request, app_label: str, model_name: str, pk: str):
    """``{"password1", "password2", "usable_password"?}`` (admin's password page)."""
    opts = _opts(app_label, model_name)
    return users.change_password(opts, request, pk, _body(request))


# --- saved list views (/api/list-views) -----------------------------------------------


@views_router.get("")
def list_views(request, model: str):
    return {"views": listviews.list_views(request, model)}


@views_router.post("", response={201: dict})
def create_view(request):
    return Status(201, listviews.create(request, _body(request)))


@views_router.patch("/{view_id}")
def update_view(request, view_id: int):
    return listviews.update(request, view_id, _body(request))


@views_router.delete("/{view_id}", response={204: None})
def delete_view(request, view_id: int):
    listviews.delete(request, view_id)
    return Status(204, None)


# --- global search (/api/search) ------------------------------------------------------


@search_router.get("")
def global_search(request, q: str = "", per_model: int = 0):
    """Every model the user may view, searched with its own ``search_fields``."""
    if len(q) > 200:
        raise service.CrudError(400, _("The search text is too long"))
    return search.global_search(request, q, max(0, min(per_model, 20)) or None)
