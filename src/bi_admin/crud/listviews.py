"""Saved list views: a user's named search, filters, ordering and columns for one
model's list. Private to the owner; applying one is just loading its query string,
so the list's own permission, scope and validation rules apply when it runs."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from django.utils.translation import gettext as _

from bi_admin.crud.service import CrudError, available_columns, get_options, require
from bi_admin.models import SavedListView

MAX_VIEWS = 50  # per user and model


def as_dict(view: SavedListView) -> dict[str, Any]:
    return {
        "id": view.pk,
        "model": view.model_label,
        "name": view.name,
        "params": view.params,
        "columns": view.columns,
        "is_default": view.is_default,
        "updated_at": view.updated_at.isoformat(),
    }


def _clean(options, data: dict[str, Any], partial: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    errors: dict[str, list[str]] = {}
    if "name" in data or not partial:
        name = str(data.get("name") or "").strip()
        if not name:
            errors["name"] = [_("This field is required.")]
        elif len(name) > 100:
            errors["name"] = [_("Use at most 100 characters.")]
        out["name"] = name
    if "params" in data or not partial:
        params = data.get("params") or {}
        if not isinstance(params, dict) or not all(isinstance(k, str) for k in params):
            errors["params"] = [_("Must be an object of query parameters.")]
        else:
            params = {k: str(v) for k, v in params.items() if k not in ("page", "view")}
            out["params"] = params
    if "columns" in data or not partial:
        columns = data.get("columns") or []
        allowed = set(available_columns(options))
        if not isinstance(columns, list) or any(c not in allowed for c in columns):
            errors["columns"] = [_("Unknown column.")]
        else:
            out["columns"] = list(dict.fromkeys(columns))
    if "is_default" in data:
        out["is_default"] = bool(data["is_default"])
    if errors:
        raise CrudError(400, _("Validation failed"), errors=errors)
    return out


def _options(label: str, request):
    options = get_options(label)
    require(options, request, "view")
    return options


def list_views(request, label: str) -> list[dict[str, Any]]:
    options = _options(label, request)
    views = SavedListView.objects.filter(owner=request.user, model_label=options.label)
    return [as_dict(v) for v in views]


def _save(view: SavedListView, request) -> None:
    try:
        with transaction.atomic():
            if view.is_default:
                SavedListView.objects.filter(
                    owner=request.user, model_label=view.model_label, is_default=True
                ).exclude(pk=view.pk).update(is_default=False)
            view.save()
    except IntegrityError:
        raise CrudError(
            400,
            _("Validation failed"),
            errors={"name": [_("You already have a view with this name.")]},
        ) from None


def create(request, data: dict[str, Any]) -> dict[str, Any]:
    options = _options(str(data.get("model") or ""), request)
    if (
        SavedListView.objects.filter(owner=request.user, model_label=options.label).count()
        >= MAX_VIEWS
    ):
        raise CrudError(400, _("You have too many saved views for this list."))
    values = _clean(options, data, partial=False)
    view = SavedListView(owner=request.user, model_label=options.label, **values)
    _save(view, request)
    return as_dict(view)


def _own(request, view_id: int) -> SavedListView:
    view = SavedListView.objects.filter(owner=request.user, pk=view_id).first()
    if view is None:
        raise CrudError(404, _("Not found"))
    _options(view.model_label, request)
    return view


def update(request, view_id: int, data: dict[str, Any]) -> dict[str, Any]:
    view = _own(request, view_id)
    for key, value in _clean(get_options(view.model_label), data, partial=True).items():
        setattr(view, key, value)
    _save(view, request)
    return as_dict(view)


def delete(request, view_id: int) -> None:
    _own(request, view_id).delete()
