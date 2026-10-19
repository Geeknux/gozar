import json

from django.utils.translation import gettext as _
from ninja import Router, Status

from bi_admin.crud import service

router = Router(tags=["crud"])


def _body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body)
    except ValueError:
        raise service.CrudError(400, _("Invalid JSON body")) from None


def _opts(app_label: str, model_name: str):
    return service.get_options(f"{app_label}.{model_name}")


@router.get("/{app_label}/{model_name}/")
def list_objects(request, app_label: str, model_name: str):
    """Query params: q, page, page_size, ordering, and filters like ``status=paid``,
    ``created_at__date__gte=2025-01-01``, ``customer__city=3``, ``paid_at__isnull=true``."""
    opts = _opts(app_label, model_name)
    return service.list_objects(opts, request, request.GET.dict())


@router.post("/{app_label}/{model_name}/", response={201: dict})
def create_object(request, app_label: str, model_name: str):
    opts = _opts(app_label, model_name)
    obj = service.save_object(opts, request, _body(request))
    return Status(201, service.serialize_object(opts, obj, request))


@router.get("/{app_label}/{model_name}/new")
def new_object(request, app_label: str, model_name: str):
    """Default values for an empty create form."""
    opts = _opts(app_label, model_name)
    service.require(opts, request, "add")
    return service.serialize_object(opts, opts.model(), request)


@router.get("/{app_label}/{model_name}/autocomplete")
def autocomplete(request, app_label: str, model_name: str, field: str, q: str = ""):
    opts = _opts(app_label, model_name)
    return service.autocomplete(opts, request, field, q)


@router.post("/{app_label}/{model_name}/actions/{action}")
def run_action(request, app_label: str, model_name: str, action: str):
    opts = _opts(app_label, model_name)
    body = _body(request)
    return service.run_action(opts, request, action, body.get("pks", []))


@router.post("/{app_label}/{model_name}/delete-plan")
def bulk_delete_plan(request, app_label: str, model_name: str):
    opts = _opts(app_label, model_name)
    return service.action_plan(opts, request, _body(request).get("pks", []))


@router.get("/{app_label}/{model_name}/{pk}")
def get_object(request, app_label: str, model_name: str, pk: str):
    opts = _opts(app_label, model_name)
    service.require(opts, request, "view")
    return service.serialize_object(opts, service.get_object(opts, request, pk), request)


@router.patch("/{app_label}/{model_name}/{pk}")
def update_object(request, app_label: str, model_name: str, pk: str):
    opts = _opts(app_label, model_name)
    service.require(opts, request, "change")
    obj = service.get_object(opts, request, pk)
    obj = service.save_object(opts, request, _body(request), obj, partial=True)
    return service.serialize_object(opts, obj, request)


@router.put("/{app_label}/{model_name}/{pk}")
def replace_object(request, app_label: str, model_name: str, pk: str):
    opts = _opts(app_label, model_name)
    service.require(opts, request, "change")
    obj = service.get_object(opts, request, pk)
    obj = service.save_object(opts, request, _body(request), obj, partial=False)
    return service.serialize_object(opts, obj, request)


@router.get("/{app_label}/{model_name}/{pk}/delete-plan")
def delete_plan(request, app_label: str, model_name: str, pk: str):
    opts = _opts(app_label, model_name)
    service.require(opts, request, "delete")
    obj = service.get_object(opts, request, pk)
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
