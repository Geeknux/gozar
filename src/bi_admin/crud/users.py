"""The user admin's own pages: change password and the permission picker tree."""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.contrib.auth import password_validation, update_session_auth_hash
from django.contrib.auth.models import Permission
from django.utils.encoding import force_str
from django.utils.text import capfirst
from django.utils.translation import gettext as _

from bi_admin.access.rules import target_queryset
from bi_admin.crud.service import (
    CrudError,
    form_errors,
    get_object,
    log_change,
    require,
    serialize_object,
)
from bi_admin.discovery import model_label
from bi_admin.models import ChangeLog

DEFAULT_ACTIONS = ("view", "add", "change", "delete")


def password_help() -> list[str]:
    return [force_str(t) for t in password_validation.password_validators_help_texts()]


def change_password(options, request, pk: str, data: dict[str, Any]) -> dict[str, Any]:
    """Admin's "Change password" page: two password fields, the validators, and the
    option of disabling password-based login (``usable_password``)."""
    if options.change_password_form is None:
        raise CrudError(404, _("This model has no password page"))
    obj = get_object(options, request, pk)
    require(options, request, "change", obj)
    if not isinstance(data, dict):
        raise CrudError(400, _("Request body must be a JSON object"))
    form = options.change_password_form(obj, data={k: v for k, v in data.items() if v is not None})
    if not form.is_valid():
        raise CrudError(400, _("Validation failed"), errors=form_errors(form))
    form.save()
    log_change(request, obj, ChangeLog.Action.CHANGE, {"fields": ["password"]})
    if obj.pk == request.user.pk:
        update_session_auth_hash(request, obj)  # changing your own password keeps you in
    return serialize_object(options, obj, request)


def password_form(options) -> dict[str, Any]:
    if options.change_password_form is None:
        raise CrudError(404, _("This model has no password page"))
    from bi_admin.crud.forms import form_field_meta

    form = options.change_password_form(options.model())
    names = sorted(form.fields, key=lambda n: n != "usable_password")  # the choice first
    return {
        "fields": [form_field_meta(n, form.fields[n]) for n in names],
        "help": password_help(),
    }


def permission_tree(options, request, field_name: str) -> dict[str, Any]:
    """Every permission a many-to-many field to ``auth.Permission`` may hold, as
    apps → models → permissions with readable labels."""
    require(options, request, "view")
    try:
        f = options.model._meta.get_field(field_name)
    except Exception:
        f = None
    if f is None or not f.many_to_many or f.related_model is not Permission:
        raise CrudError(400, _("%(field)r is not a permission field") % {"field": field_name})
    if field_name not in options.get_fields():
        raise CrudError(400, _("%(field)r is not a permission field") % {"field": field_name})
    perms = target_queryset(model_label(Permission), request).select_related("content_type")
    action_labels = {
        "view": _("View"),
        "add": _("Add"),
        "change": _("Change"),
        "delete": _("Delete"),
    }
    tree: dict[str, dict[str, Any]] = {}
    for perm in perms.order_by("content_type__app_label", "content_type__model", "codename"):
        ct = perm.content_type
        try:
            app_config = apps.get_app_config(ct.app_label)
            app_name = capfirst(force_str(app_config.verbose_name))
        except LookupError:
            app_name = ct.app_label
        model = ct.model_class()
        app = tree.setdefault(
            ct.app_label, {"app_label": ct.app_label, "verbose_name": app_name, "models": {}}
        )
        entry = app["models"].setdefault(
            ct.model,
            {
                "model": f"{ct.app_label}.{ct.model}",
                "verbose_name": capfirst(force_str(model._meta.verbose_name_plural))
                if model
                else ct.model,
                "permissions": [],
            },
        )
        action = perm.codename.split("_", 1)[0]
        default = perm.codename == f"{action}_{ct.model}" and action in DEFAULT_ACTIONS
        entry["permissions"].append(
            {
                "pk": perm.pk,
                "codename": f"{ct.app_label}.{perm.codename}",
                "label": force_str(action_labels[action]) if default else force_str(perm.name),
                "action": action if default else None,
            }
        )
    order = {a: i for i, a in enumerate(DEFAULT_ACTIONS)}
    out = []
    for app in sorted(tree.values(), key=lambda a: a["verbose_name"].lower()):
        models = sorted(app["models"].values(), key=lambda m: m["verbose_name"].lower())
        for m in models:
            m["permissions"].sort(key=lambda p: (order.get(p["action"], 9), p["label"]))
        out.append({**app, "models": models})
    return {"field": field_name, "apps": out}
