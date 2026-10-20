"""Inline editing: related rows saved with their parent (admin's inline formsets).

A save carries the rows under ``"__inlines__": {"<inline name>": [row, ...]}``,
each row ``{"pk": <pk or null>, "values": {field: value}, "delete": bool}``.
Rows that are left out stay as they are. The server builds a Django inline
formset over *every* visible row of the parent (submitted values merged over the
stored ones), so ``min_num``/``max_num`` and the formset's cross-row unique
checks see the whole set; nothing is saved unless the parent and every inline
validate, and all of it happens in one transaction.

Row-level rules: an inline lists only the rows of its model that the user may see
(``hidden`` counts the rest, which saving never touches); relation fields only
accept rows in scope; a saved row that would end up outside the scope rolls the
whole save back (``403 out_of_scope``); deleting a row runs the same deletion
plan as any delete. Permissions are the inline model's (add/change/delete), like
admin; an auto-created many-to-many through model follows the parent's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.db import models
from django.forms.models import ModelForm, inlineformset_factory
from django.utils.datastructures import MultiValueDict
from django.utils.encoding import force_str
from django.utils.text import capfirst
from django.utils.translation import gettext as _

from bi_admin.access.rules import model_filter, restrict
from bi_admin.crud import forms as crud_forms
from bi_admin.crud.options import InlineOptions, ModelOptions
from bi_admin.discovery import get_registry
from bi_admin.models import ChangeLog


def _service():
    from bi_admin.crud import service

    return service


def base_queryset(inline: InlineOptions, request) -> models.QuerySet:
    return restrict(inline.get_queryset(request), model_filter(inline.model, request))


def visible_rows(inline: InlineOptions, request, parent) -> models.QuerySet:
    if parent is None or parent.pk is None:
        return inline.model._default_manager.none()
    qs = base_queryset(inline, request).filter(**{inline.fk.name: parent})
    return qs.order_by(*inline.get_ordering())


def field_metas(inline: InlineOptions) -> list[dict[str, Any]]:
    info = inline.info
    names = inline.get_fields()
    out = [info.fields[n].as_dict() for n in names if n in info.fields]
    for name, fn in inline.computed_fields().items():
        if name in names:
            out.append(computed_meta(name, fn))
    return out


def computed_meta(name: str, fn) -> dict[str, Any]:
    label = getattr(fn, "short_description", None) or name.replace("_", " ")
    return {
        "name": name,
        "verbose_name": force_str(label),
        "kind": "boolean" if getattr(fn, "boolean", False) else "computed",
        "internal_type": "",
        "primary_key": False,
        "editable": False,
        "null": True,
        "blank": True,
        "unique": False,
        "required": False,
        "has_default": False,
        "max_length": None,
        "max_digits": None,
        "decimal_places": None,
        "help_text": "",
        "choices": None,
        "related_model": None,
        "db_index": False,
        "computed": True,
        "boolean": bool(getattr(fn, "boolean", False)),
    }


def meta(options: ModelOptions, request) -> list[dict[str, Any]]:
    out = []
    reg = get_registry()
    for inline in options.get_inlines():
        perms = inline.permissions(request)
        if not perms["view"]:
            continue
        opts = inline.model._meta
        names = inline.get_fields()
        out.append(
            {
                "name": inline.get_name(),
                "model": inline.label,
                "style": inline.style,
                "verbose_name": capfirst(force_str(inline.verbose_name or opts.verbose_name)),
                "verbose_name_plural": capfirst(
                    force_str(inline.verbose_name_plural or opts.verbose_name_plural)
                ),
                "fields": names,
                "readonly_fields": inline.get_readonly_fields(),
                "field_meta": field_metas(inline),
                "widgets": {n: inline.widget_for(n) for n in names},
                "extra": inline.extra,
                "min_num": inline.min_num,
                "max_num": inline.max_num,
                "can_delete": inline.can_delete,
                "permissions": perms,
                "show_change_link": bool(inline.show_change_link and inline.label in reg),
                "pk_name": opts.pk.name,
                "defaults": _defaults(inline, request),
            }
        )
    return out


def _defaults(inline: InlineOptions, request) -> dict[str, Any]:
    """Values of a new row (the model's field defaults), shown in empty rows."""
    service = _service()
    blank = inline.model()
    return {n: service.field_value(inline, request, blank, n)[0] for n in inline.get_form_fields()}


def serialize(options: ModelOptions, request, parent) -> dict[str, Any]:
    service = _service()
    out: dict[str, Any] = {}
    for inline in options.get_inlines():
        if not inline.has_view_permission(request, parent):
            continue
        names = inline.get_fields()
        rows = []
        visible = list(visible_rows(inline, request, parent))
        for obj in visible:
            values = {}
            for name in names:
                values[name], _hidden = service.field_value(inline, request, obj, name)
            rows.append(
                {"pk": service.json_value(obj.pk), "label": force_str(obj), "values": values}
            )
        hidden = 0
        if parent is not None and parent.pk is not None:
            total = inline.model._default_manager.filter(**{inline.fk.name: parent}).count()
            hidden = total - len(visible)
        out[inline.get_name()] = {"rows": rows, "hidden": hidden}
    return out


def relation_target(options: ModelOptions, request, inline_name: str, field_name: str) -> str:
    """The model an inline's relation field points at (for its autocomplete)."""
    from bi_admin.discovery import model_label

    service = _service()
    for inline in options.get_inlines():
        if inline.get_name() != inline_name:
            continue
        if not inline.has_view_permission(request):
            raise service.CrudError(403, _("Permission denied"))
        if field_name not in inline.get_form_fields():
            break
        f = inline.model._meta.get_field(field_name)
        if not f.is_relation or f.related_model is None:
            break
        return model_label(f.related_model)
    raise service.CrudError(
        400,
        _("%(field)r is not a relation field of %(model)s")
        % {"field": field_name, "model": inline_name},
    )


# --- saving --------------------------------------------------------------------------


@dataclass
class Submission:
    inline: InlineOptions
    formset: Any
    rows: dict[int, int]  # form index → index of the row in the request
    deleted: list[models.Model] = field(default_factory=list)
    before: dict[Any, dict] = field(default_factory=dict)  # pk → snapshot of changed rows


def build_all(options, request, parent, payload, files: MultiValueDict, source=None):
    """Validated formsets for the submitted inlines and their errors by inline name:
    ``{"items": {"rows": {"<row index>": {field: [msg]}}, "non_form": [msg]}}``."""
    service = _service()
    if not isinstance(payload, dict):
        raise service.CrudError(400, _("%(key)s must be an object") % {"key": "__inlines__"})
    inlines = {i.get_name(): i for i in options.get_inlines()}
    unknown = sorted(set(payload) - set(inlines))
    if unknown:
        raise service.CrudError(
            400, _("Unknown inline %(name)r") % {"name": unknown[0]}, code="unknown_inline"
        )
    submissions, errors = [], {}
    for name, rows in payload.items():
        sub, errs = build(inlines[name], request, parent, rows, files, source)
        if sub is not None:
            submissions.append(sub)
        if errs["rows"] or errs["non_form"]:
            errors[name] = errs
    return submissions, errors


def _copyable_files(inline, request, source) -> set[str]:
    """File names on the visible rows of the object being copied ("save as new")."""
    if source is None:
        return set()
    names = set()
    file_fields = crud_forms.file_fields(inline.model, inline.get_form_fields())
    for obj in visible_rows(inline, request, source):
        for f in file_fields:
            if getattr(obj, f.name):
                names.add(getattr(obj, f.name).name)
    return names


def build(inline: InlineOptions, request, parent, rows, files: MultiValueDict, source=None):
    service = _service()
    name = inline.get_name()
    errors: dict[str, Any] = {"rows": {}, "non_form": []}

    def row_error(index, field_name, message):
        errors["rows"].setdefault(str(index), {}).setdefault(field_name, []).append(message)

    if not isinstance(rows, list):
        raise service.CrudError(400, _("Inline rows must be a list"))
    perms = inline.permissions(request, parent)
    if not perms["view"]:
        raise service.CrudError(
            403,
            _("Permission denied: %(perm)s %(model)s") % {"perm": "view", "model": inline.label},
        )
    fields = inline.get_form_fields()
    existing = list(visible_rows(inline, request, parent))
    by_pk = {str(o.pk): o for o in existing}
    submitted: dict[str, tuple[int, dict]] = {}
    new_rows: list[tuple[int, dict]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise service.CrudError(400, _("Each inline row must be an object"))
        values = row.get("values") or {}
        if not isinstance(values, dict):
            raise service.CrudError(400, _("Inline row values must be an object"))
        unknown = sorted(set(values) - set(fields))
        for key in unknown:
            row_error(index, key, _("This field cannot be edited."))
        pk = row.get("pk")
        if pk in (None, ""):
            new_rows.append((index, row))
        elif str(pk) in by_pk:
            submitted[str(pk)] = (index, row)
        else:
            row_error(
                index, "__all__", _("This row no longer exists or is outside your access scope.")
            )
    if errors["rows"]:
        return None, errors

    prefix = name
    pk_name = inline.model._meta.pk.name
    data: dict[str, Any] = {}
    form_files: dict[str, Any] = {}
    initial_extra: list[dict[str, Any]] = []
    row_map: dict[int, int] = {}
    copyable = _copyable_files(inline, request, source)
    file_errors: dict[int, dict[str, list[str]]] = {}
    i = 0

    def put(i, index, values, raw_files, obj):
        for key, value in values.items():
            data[f"{prefix}-{i}-{key}"] = value
        errs: dict[str, list[str]] = {}
        form_files.update(
            crud_forms.apply_files(
                inline.model,
                fields,
                raw_files,
                data,
                files,
                lambda n: f"{name}.{index}.{n}",
                errs,
                prefix=f"{prefix}-{i}-",
                instance=obj,
            )
        )
        if errs and index is not None:
            file_errors[index] = errs

    file_names = {f.name for f in crud_forms.file_fields(inline.model, fields)}
    for obj in existing:
        values = service._initial_data(inline.model, fields, obj)
        entry = submitted.get(str(obj.pk))
        raw_files: dict[str, Any] = {}
        index = None
        if entry is not None:
            index, row = entry
            row_map[i] = index
            submitted_values = dict(row.get("values") or {})
            raw_files = {
                k: submitted_values.pop(k) for k in list(submitted_values) if k in file_names
            }
            values.update(service.normalize_payload(submitted_values))
            if row.get("delete"):
                data[f"{prefix}-{i}-DELETE"] = "on"
        put(i, index, values, raw_files, obj)
        data[f"{prefix}-{i}-{pk_name}"] = obj.pk
        i += 1
    for index, row in new_rows:
        if row.get("delete"):
            continue  # added and removed again before saving
        row_map[i] = index
        submitted_values = dict(row.get("values") or {})
        raw_files = {k: submitted_values.pop(k) for k in list(submitted_values) if k in file_names}
        initial = {}
        for key, value in raw_files.items():  # save as new: reuse the copied row's file
            if isinstance(value, dict) and value.get("name") in copyable:
                initial[key] = value["name"]
        initial_extra.append(initial)
        # a new row starts from the model's defaults, as admin's rendered extra forms do,
        # so an untouched row stays unchanged and is skipped
        values = service._initial_data(inline.model, fields, inline.model())
        values.update(service.normalize_payload(submitted_values))
        put(i, index, values, raw_files, None)
        i += 1
    data[f"{prefix}-TOTAL_FORMS"] = str(i)
    data[f"{prefix}-INITIAL_FORMS"] = str(len(existing))

    formset_class = inlineformset_factory(
        inline.parent_model,
        inline.model,
        form=inline.form or ModelForm,
        fk_name=inline.fk.name,
        fields=fields,
        extra=0,
        can_delete=True,  # deletions are checked against can_delete and permissions below
        min_num=inline.min_num or 0,
        max_num=inline.max_num,
        validate_min=inline.min_num is not None,
        validate_max=inline.max_num is not None,
    )
    formset = formset_class(
        data=data,
        files=form_files,
        instance=parent,
        prefix=prefix,
        queryset=base_queryset(inline, request),
        initial=initial_extra or None,
    )
    for form in formset.forms:
        service.scope_relation_fields(form, request, form.instance)
    sub = Submission(inline, formset, row_map)
    valid = formset.is_valid()
    if not valid:
        for i, form_errs in enumerate(formset.errors):
            if form_errs:
                index = row_map.get(i, i)
                for key, msgs in form_errs.items():
                    for msg in msgs:
                        row_error(index, key, force_str(msg))
        errors["non_form"] = [force_str(e) for e in formset.non_form_errors()]
    for index, errs in file_errors.items():
        for key, msgs in errs.items():
            for msg in msgs:
                row_error(index, key, msg)

    initial_count = formset.initial_form_count()
    for i, form in enumerate(formset.forms):
        index = row_map.get(i)
        is_new = i >= initial_count
        deleted = formset._should_delete_form(form)
        if deleted and not is_new:
            if not inline.has_delete_permission(request, parent):
                row_error(index, "__all__", _("You can't delete these rows."))
                continue
            sub.deleted.append(form.instance)
        elif is_new and form.has_changed() and not perms["add"]:
            row_error(index, "__all__", _("You can't add these rows."))
        elif not is_new and not deleted and form.has_changed():
            if not perms["change"]:
                row_error(index, "__all__", _("You can't change these rows."))
            else:
                sub.before[form.instance.pk] = service.snapshot(inline.model, fields, existing[i])
    if sub.deleted:
        plan = service.deletion_plan(request, sub.deleted, inline.model)
        if not plan["deletable"]:
            for i, form in enumerate(formset.forms):
                if form.instance in sub.deleted:
                    row_error(row_map.get(i), "__all__", _("This row can't be deleted."))
            errors["delete_plan"] = plan
    return sub, errors


def save_all(options, request, form, submissions: list[Submission], change: bool) -> list[dict]:
    """Save the validated formsets (inside the parent's transaction) and return the
    change-log summary. Rows that end up outside the user's scope roll it all back."""
    service = _service()
    summary = []
    for sub in submissions:
        inline, formset = sub.inline, sub.formset
        formset.instance = form.instance
        deleted = [(force_str(o), o.pk) for o in sub.deleted]
        for obj in sub.deleted:
            service.log_change(request, obj, ChangeLog.Action.DELETE, {})
        options.save_formset(request, form, formset, change)
        saved = [*formset.new_objects, *(obj for obj, _f in formset.changed_objects)]
        q = model_filter(inline.model, request)
        if q is not None and saved:
            pks = [o.pk for o in saved]
            if inline.model._base_manager.filter(q, pk__in=pks).count() != len(set(pks)):
                raise service.CrudError(
                    403,
                    _("You can't save a record outside your access scope."),
                    code="out_of_scope",
                )
        fields = inline.get_form_fields()
        changed = []
        for obj, changed_fields in formset.changed_objects:
            after = service.snapshot(inline.model, fields, obj)
            before = sub.before.get(obj.pk, {})
            names = [n for n in after if before.get(n) != after[n]] or list(changed_fields)
            diff = service.build_diff(inline.model, before, after, names)
            service.log_change(
                request, obj, ChangeLog.Action.CHANGE, {"fields": names, "diff": diff}
            )
            changed.append(
                {"label": force_str(obj), "pk": force_str(obj.pk), "fields": names, "diff": diff}
            )
        for obj in formset.new_objects:
            service.log_change(request, obj, ChangeLog.Action.ADD, {})
        if formset.new_objects or changed or deleted:
            summary.append(
                {
                    "name": inline.get_name(),
                    "model": inline.label,
                    "verbose_name": force_str(inline.model._meta.verbose_name),
                    "added": [force_str(o) for o in formset.new_objects],
                    "changed": changed,
                    "deleted": [label for label, _pk in deleted],
                }
            )
    return summary
