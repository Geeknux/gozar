"""Form plumbing for CRUD saves: request payloads (JSON or multipart with files),
form classes, file-field data and metadata for form-only fields.

A save is JSON (``{"field": value, ...}``) unless it uploads files: then it is
``multipart/form-data`` with the same JSON object in a ``__payload__`` part and
one part per uploaded file, named by the field (``image``) or, inside an inline,
``<inline>.<row index>.<field>`` (``images.0.image``). In the JSON, a file field
is left out (or sent back as the ``{"name": ...}`` it was read as) to keep the
current file, and ``null`` clears it.
"""

from __future__ import annotations

import json
from typing import Any

from django import forms
from django.core.files.uploadedfile import UploadedFile
from django.db import models
from django.forms.models import ModelForm, modelform_factory
from django.utils.datastructures import MultiValueDict
from django.utils.encoding import force_str
from django.utils.translation import gettext as _

from bi_admin.conf import setting

PAYLOAD_PART = "__payload__"
INLINES_KEY = "__inlines__"  # can't clash: Django field names never contain "__"
SAVE_AS_KEY = "__save_as__"  # "save as new": the pk of the object being copied


class PayloadError(Exception):
    pass


def read_payload(request) -> tuple[Any, MultiValueDict]:
    """The JSON body and uploaded files of a create/update request."""
    content_type = request.META.get("CONTENT_TYPE", "")
    if content_type.startswith("multipart/form-data"):
        if request.method != "POST":  # Django only parses multipart bodies for POST
            method, request.method = request.method, "POST"
            try:
                request._load_post_and_files()
            finally:
                request.method = method
        raw = request.POST.get(PAYLOAD_PART, "")
        files = request.FILES
    else:
        raw = request.body
        files = MultiValueDict()
    if not raw:
        return {}, files
    try:
        return json.loads(raw), files
    except ValueError:
        raise PayloadError(_("Invalid JSON body")) from None


def model_form_class(model: type[models.Model], fields: list[str], form=None):
    return modelform_factory(model, form=form or ModelForm, fields=fields)


def file_fields(model: type[models.Model], names) -> list[models.FileField]:
    out = []
    for name in names:
        try:
            f = model._meta.get_field(name)
        except Exception:
            continue
        if isinstance(f, models.FileField):
            out.append(f)
    return out


def apply_files(
    model: type[models.Model],
    names,
    payload: dict[str, Any],
    data: dict[str, Any],
    files: MultiValueDict,
    upload_key,
    errors: dict[str, list[str]],
    prefix: str = "",
    instance: models.Model | None = None,
) -> dict[str, UploadedFile]:
    """Turn file-field values of ``payload`` into form ``data``/``files`` entries.

    ``upload_key(name)`` is the multipart part holding a new file for ``name``.
    Returns the uploads given to the form (keyed by form field name)."""
    form_files: dict[str, UploadedFile] = {}
    limit = setting("UPLOAD_MAX_BYTES")
    for f in file_fields(model, names):
        key = f"{prefix}{f.name}"
        upload = files.get(upload_key(f.name))
        value = payload.get(f.name, ...)
        data.pop(key, None)
        if upload is not None:
            if limit and upload.size > limit:
                errors.setdefault(f.name, []).append(
                    _("The file is too large (%(size)s; the limit is %(limit)s).")
                    % {"size": filesize(upload.size), "limit": filesize(limit)}
                )
                continue
            form_files[key] = upload
        elif value is None or value is False:
            data[f"{key}-clear"] = "on"
            # clearing a required file is an error (the form would silently keep it);
            # without a current file the form itself reports the missing value
            if not f.blank and instance is not None and getattr(instance, f.name):
                errors.setdefault(f.name, []).append(force_str(f.error_messages["blank"]))
    return form_files


def filesize(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def form_field_meta(name: str, field: forms.Field) -> dict[str, Any]:
    """FieldMeta for a field that only exists on a form (``password1`` on the add-user
    form, ``usable_password``), described like a model field for the SPA."""
    widget = field.widget
    if isinstance(widget, forms.PasswordInput):
        kind = "password"
    elif isinstance(field, forms.BooleanField):
        kind = "boolean"
    elif isinstance(field, forms.IntegerField):
        kind = "integer"
    elif isinstance(field, forms.DateTimeField):
        kind = "datetime"
    elif isinstance(field, forms.DateField):
        kind = "date"
    else:
        kind = "string"
    choices = None
    if isinstance(field, forms.ChoiceField):
        choices = [{"value": v, "label": force_str(label)} for v, label in field.choices]
    return {
        "name": name,
        "verbose_name": force_str(field.label or name.replace("_", " ")),
        "kind": kind,
        "internal_type": type(field).__name__,
        "primary_key": False,
        "editable": True,
        "null": False,
        "blank": not field.required,
        "unique": False,
        "required": field.required,
        "has_default": field.initial is not None,
        "initial": field.initial if isinstance(field.initial, str | bool | int) else None,
        "max_length": getattr(field, "max_length", None),
        "max_digits": None,
        "decimal_places": None,
        "help_text": force_str(field.help_text or ""),
        "choices": choices,
        "related_model": None,
        "db_index": False,
        "form_only": True,
        "radio": isinstance(widget, forms.RadioSelect),
    }
