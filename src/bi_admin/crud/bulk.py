"""Editing many rows at once: ``list_editable`` (cells edited on the list page) and
bulk edit (the same values written to every selected row).

Each row goes through ``save_object`` restricted to the allowed fields, so the
form's validation, relation scoping, the out-of-scope check and the change log
apply exactly as on the change form. Everything happens in one transaction: if
any row fails, nothing is saved and the errors come back per row.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import router, transaction
from django.utils.encoding import force_str
from django.utils.translation import gettext as _

from bi_admin.access.rules import model_queryset
from bi_admin.crud.service import (
    CrudError,
    chosen_columns,
    json_value,
    require,
    save_object,
    selection,
    serialize_rows,
)


class _Rollback(Exception):
    pass


def _save_rows(options, request, items, fields, log_extra) -> tuple[dict, list]:
    """Save ``[(obj, values), ...]``; returns per-row errors and the saved objects."""
    errors: dict[str, Any] = {}
    saved = []
    try:
        with transaction.atomic(using=router.db_for_write(options.model)):
            for obj, values in items:
                key = force_str(obj.pk)
                try:
                    saved.append(
                        save_object(
                            options,
                            request,
                            values,
                            obj,
                            partial=True,
                            fields=fields,
                            log_extra=log_extra,
                        )
                    )
                except CrudError as exc:
                    if exc.status not in (400, 403):
                        raise
                    errors[key] = exc.payload.get("errors") or {"__all__": [exc.payload["detail"]]}
            if errors:
                raise _Rollback
    except _Rollback:
        saved = []
    return errors, saved


def update_rows(options, request, rows: Any) -> dict[str, Any]:
    """``PATCH`` on the list: ``{"rows": [{"pk": ..., "values": {field: value}}]}`` for
    the ``list_editable`` fields."""
    require(options, request, "change")
    editable = options.get_list_editable()
    if not editable:
        raise CrudError(400, _("This list has no editable columns"))
    if not isinstance(rows, list) or not rows:
        raise CrudError(400, _("Nothing to save"))
    if len(rows) > options.list_max_page_size:
        raise CrudError(400, _("Too many rows in one save"))
    pk_field = options.model._meta.pk
    wanted = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("values"), dict):
            raise CrudError(400, _("Each row must be an object with pk and values"))
        try:
            wanted[force_str(pk_field.to_python(row.get("pk")))] = row["values"]
        except (ValidationError, ValueError, TypeError):
            raise CrudError(400, _("Invalid primary key in selection")) from None
    objs = {
        force_str(o.pk): o for o in model_queryset(options, request).filter(pk__in=list(wanted))
    }
    missing = {pk: {"__all__": [_("Not found")]} for pk in wanted if pk not in objs}
    if missing:
        raise CrudError(400, _("Validation failed"), row_errors=missing)
    items = [(objs[pk], values) for pk, values in wanted.items()]
    errors, saved = _save_rows(options, request, items, editable, {"source": "list"})
    if errors:
        raise CrudError(400, _("Validation failed"), row_errors=errors)
    cols = chosen_columns(options, {})
    fresh = list(model_queryset(options, request).filter(pk__in=[o.pk for o in saved]))
    out_rows = serialize_rows(options, request, cols, fresh)
    from bi_admin.crud.service import field_value

    for row, obj in zip(out_rows, fresh, strict=True):
        row["edit"] = {n: field_value(options, request, obj, n)[0] for n in editable}
    return {"updated": len(saved), "rows": out_rows}


def bulk_edit(options, request, body: Any) -> dict[str, Any]:
    """``{"pks": [...] | "all": true + "params", "values": {field: value}}``: write the
    same values to every selected row the user may see."""
    require(options, request, "change")
    if not isinstance(body, dict):
        raise CrudError(400, _("Request body must be a JSON object"))
    values = body.get("values")
    if not isinstance(values, dict) or not values:
        raise CrudError(400, _("Choose at least one field to change"))
    allowed = options.get_bulk_edit_fields()
    bad = sorted(set(values) - set(allowed))
    if bad:
        raise CrudError(
            400,
            _("Unknown or read-only fields"),
            errors={k: [_("This field cannot be edited.")] for k in bad},
        )
    objs = list(selection(options, request, body))
    if not objs:
        raise CrudError(400, _("Select at least one row"))
    errors, saved = _save_rows(
        options, request, [(o, dict(values)) for o in objs], list(values), {"source": "bulk"}
    )
    if errors:
        first = next(iter(errors.values()))
        raise CrudError(400, _("Validation failed"), row_errors=errors, errors=first)
    return {"updated": len(saved), "pks": [json_value(o.pk) for o in saved]}
