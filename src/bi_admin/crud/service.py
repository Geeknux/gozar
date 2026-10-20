"""CRUD service layer: listing, filtering, serialization, validation, deletion.

The API layer stays thin; everything that touches the ORM lives here so the
same rules apply to every caller (JSON API, report drill-down, import, bulk edit).
"""

from __future__ import annotations

import datetime as dt
import decimal
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from django.contrib.auth import password_validation
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.paginator import EmptyPage, Paginator
from django.db import models, router, transaction
from django.db.models import Q
from django.db.models.deletion import Collector, ProtectedError, RestrictedError
from django.db.models.fields.files import FieldFile
from django.forms.models import model_to_dict
from django.utils.datastructures import MultiValueDict
from django.utils.encoding import force_str
from django.utils.text import capfirst
from django.utils.translation import gettext as _

from bi_admin.access.rules import model_filter, model_queryset, target_queryset
from bi_admin.crud import forms as crud_forms
from bi_admin.crud.options import ModelOptions, site
from bi_admin.discovery import PathStep, get_registry, model_label
from bi_admin.execution import cache
from bi_admin.models import ChangeLog

logger = logging.getLogger("bi_admin.crud")

# Query parameters of the list that aren't field filters.
RESERVED_PARAMS = {
    "q",
    "page",
    "page_size",
    "ordering",
    "columns",
    "view",
    "dh_year",
    "dh_month",
    "dh_day",
    "dh_calendar",
}
FILTER_LOOKUPS = {
    "exact",
    "iexact",
    "in",
    "gt",
    "gte",
    "lt",
    "lte",
    "isnull",
    "icontains",
    "istartswith",
    "date",
    "date__gte",
    "date__lte",
    "year",
    "month",
    "day",
}
AUTOCOMPLETE_LIMIT = 20
AUTOCOMPLETE_MAX_LIMIT = 200  # the dual-list selector asks for more at once
FILTER_CHOICES_LIMIT = 100
DIFF_TEXT_LIMIT = 500  # characters of a value kept in a history diff

# Arabic code points that Persian text often contains by mistake.
_ARABIC_TO_PERSIAN = str.maketrans({"ي": "ی", "ك": "ک", "ى": "ی"})
_PERSIAN_TO_ARABIC = str.maketrans({"ی": "ي", "ک": "ك"})


class CrudError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.payload = {"detail": message, **extra}


# --- options / permissions ---------------------------------------------------------


def get_options(label: str) -> ModelOptions:
    try:
        return site.options_for(label)
    except LookupError:
        raise CrudError(404, _("Unknown model %(model)r") % {"model": label}) from None


def require(options: ModelOptions, request, perm: str, obj=None) -> None:
    allowed = options.has_perm(request, perm)
    if allowed and obj is not None:
        check = {
            "change": options.has_change_permission,
            "delete": options.has_delete_permission,
        }.get(perm)
        allowed = check(request, obj) if check else True
    if not allowed:
        raise CrudError(
            403,
            _("Permission denied: %(perm)s %(model)s") % {"perm": perm, "model": options.label},
        )


def visible_models(request) -> list[ModelOptions]:
    out = []
    for info in get_registry():
        opts = site.options_for(info.label)
        if opts.show_in_menu and opts.has_view_permission(request):
            out.append(opts)
    return out


# --- value serialization -----------------------------------------------------------


def obj_ref(obj: models.Model) -> dict[str, Any]:
    return {"pk": json_value(obj.pk), "label": force_str(obj), "model": model_label(type(obj))}


def json_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, models.Model):
        return obj_ref(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, FieldFile):
        if not value:
            return None
        out = {"name": value.name, "filename": value.name.rsplit("/", 1)[-1], "url": None}
        try:
            out["url"] = value.url
        except ValueError:
            pass
        return out
    if isinstance(value, dict | list | tuple):
        return value
    return force_str(value)


def django_field(model: type[models.Model], path: str) -> models.Field:
    """The Django field at the end of ``path`` (raises ``LookupError``)."""
    current = model
    parts = path.split("__")
    f = None
    for i, part in enumerate(parts):
        try:
            f = current._meta.get_field(part)
        except FieldDoesNotExist:
            raise LookupError(f"Unknown field {part!r} in {path!r}") from None
        if f.is_relation and f.related_model is not None:
            if i < len(parts) - 1:
                current = f.related_model
            elif not getattr(f, "concrete", False) or f.many_to_many:
                return f.related_model._meta.pk
    return f


def _walk(obj: models.Model, path: str) -> Any:
    value: Any = obj
    for part in path.split("__"):
        if value is None:
            return None
        value = getattr(value, part)
    return value


# --- list columns ------------------------------------------------------------------


@dataclass
class Column:
    name: str
    label: str
    kind: str
    sortable: bool
    path: str | None  # ORM path when the column is a (possibly related) field
    choices: dict | None = None
    is_link: bool = False
    boolean: bool = False
    empty_value: str = "—"
    order_path: str | None = None  # what sorting by this column orders by
    editable: bool = False
    image: bool = False
    fn: Any = field(default=None, repr=False)

    def as_dict(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "sortable": self.sortable,
            "is_link": self.is_link,
            "boolean": self.boolean,
            "empty_value": self.empty_value,
        }
        if self.editable:
            out["editable"] = True
        if self.image:
            out["image"] = True
        return out


def available_columns(options: ModelOptions) -> list[str]:
    """Columns a user may pick for the list (a saved view): ``list_display`` plus the
    other visible, listable fields of the model."""
    names = options.get_list_display()
    hidden = set(options.exclude) | set(options.password_fields)
    for f in options.info.fields.values():
        if f.name in names or f.name in hidden or f.kind in ("m2m", "json"):
            continue
        names.append(f.name)
    return names


def list_columns(options: ModelOptions, names: list[str] | None = None) -> list[Column]:
    reg = get_registry()
    links = set(options.get_list_display_links())
    editable = set(options.get_list_editable())
    sortable_by = set(options.sortable_by) if options.sortable_by is not None else None
    empty = force_str(options.empty_value_display)
    cols = []
    for name in names if names is not None else options.get_list_display():
        if name == "__str__":
            cols.append(
                Column(
                    name,
                    capfirst(options.info.verbose_name),
                    "string",
                    False,
                    None,
                    is_link=name in links,
                    empty_value=empty,
                )
            )
            continue
        try:
            steps, finfo = reg.resolve_lookup(options.label, name)
        except LookupError:
            fn = options.list_callable(name)
            if fn is None:
                raise CrudError(
                    500,
                    _("list_display entry %(name)r is not a field or callable") % {"name": name},
                ) from None
            label = getattr(fn, "short_description", None) or name.replace("_", " ")
            order = getattr(fn, "admin_order_field", None)
            cols.append(
                Column(
                    name,
                    capfirst(force_str(label)),
                    "boolean" if getattr(fn, "boolean", False) else "other",
                    bool(order) and (sortable_by is None or name in sortable_by),
                    None,
                    is_link=name in links,
                    boolean=bool(getattr(fn, "boolean", False)),
                    empty_value=force_str(getattr(fn, "empty_value_display", empty)),
                    order_path=order if isinstance(order, str) else None,
                    fn=fn,
                )
            )
            continue
        label = path_label(options.model, name)
        kind = "fk" if ends_on_relation(steps, name) else finfo.kind
        choices = dict(finfo.choices) if finfo.choices else None
        sortable = not any(s.many for s in steps) and kind != "m2m"
        cols.append(
            Column(
                name,
                label,
                kind,
                sortable and (sortable_by is None or name in sortable_by),
                name,
                choices,
                is_link=name in links,
                boolean=kind == "boolean",
                empty_value=empty,
                order_path=name,
                editable=name in editable,
                image=finfo.image,
            )
        )
    return cols


def ends_on_relation(steps, path: str) -> bool:
    return bool(steps) and len(path.split("__")) == len(steps)


def path_label(model: type[models.Model], path: str) -> str:
    """Human label for a lookup path: ``customer__city__name`` → ``Customer / City / Name``."""
    parts, current = [], model
    for part in path.split("__"):
        f = current._meta.get_field(part)
        if hasattr(f, "verbose_name"):
            parts.append(capfirst(force_str(f.verbose_name)))
        else:  # reverse relation
            parts.append(capfirst(force_str(f.related_model._meta.verbose_name_plural)))
        if f.is_relation and f.related_model is not None:
            current = f.related_model
    return " / ".join(parts)


def cell_value(options: ModelOptions, col: Column, obj: models.Model) -> Any:
    if col.name == "__str__":
        return force_str(obj)
    if col.path is None:
        # options methods are bound; list_display functions, model methods (from the
        # class) and property getters all take the object
        fn = col.fn or options.list_callable(col.name)
        return json_value(fn(obj))
    value = _walk(obj, col.path)
    if col.choices is not None and value is not None:
        return force_str(col.choices.get(value, value))
    return json_value(value)


def _hop_objects(obj: models.Model, steps) -> list[models.Model | None]:
    """The related object reached after each to-one hop of a column path."""
    out, current = [], obj
    for step in steps:
        current = getattr(current, step.relation, None) if current is not None else None
        out.append(current)
    return out


def serialize_rows(options: ModelOptions, request, cols: list[Column], objs) -> list[dict]:
    """List/drill-down rows. A cell read through a related row outside the user's
    row-level scope (``customer``, ``customer__city__name`` …) is ``restricted``."""
    objs = list(objs)
    reg = get_registry()
    hidden: dict[int, set] = {}  # column index → pks of rows whose cell is hidden
    for i, col in enumerate(cols):
        if not col.path:
            continue
        steps, finfo = reg.resolve_lookup(options.label, col.path)
        if finfo.is_relation and finfo.kind in ("fk", "o2o"):  # the column shows that row
            source = steps[-1].target if steps else options.label
            steps = [*steps, PathStep(finfo.name, source, finfo.related_model, False)]
        if not steps or any(s.many for s in steps):
            continue
        hops = {o.pk: _hop_objects(o, steps) for o in objs}
        for depth, step in enumerate(steps):
            if model_filter(step.target, request) is None:
                continue
            pks = {h[depth].pk for h in hops.values() if h[depth] is not None}
            if not pks:
                continue
            allowed = set(
                target_queryset(step.target, request)
                .filter(pk__in=pks)
                .values_list("pk", flat=True)
            )
            for row_pk, h in hops.items():
                if h[depth] is not None and h[depth].pk not in allowed:
                    hidden.setdefault(i, set()).add(row_pk)
    return [
        {
            "pk": json_value(obj.pk),
            "label": force_str(obj),
            "cells": [
                restricted_ref() if obj.pk in hidden.get(i, ()) else cell_value(options, c, obj)
                for i, c in enumerate(cols)
            ],
        }
        for obj in objs
    ]


# --- filters -----------------------------------------------------------------------


def filter_specs(options: ModelOptions, request=None) -> list[dict[str, Any]]:
    reg = get_registry()
    specs = []
    for path in options.get_list_filter():
        steps, finfo = reg.resolve_lookup(options.label, path)
        relation = finfo.is_relation or ends_on_relation(steps, path)
        spec: dict[str, Any] = {
            "name": path,
            "label": path_label(options.model, path),
            "kind": "relation" if relation else finfo.kind,
            "null": finfo.null,
        }
        if relation:
            target = finfo.related_model if finfo.is_relation else steps[-1].target
            spec["related_model"] = target
            qs = target_queryset(target, request)  # only rows the user may see
            options_ = list(qs[: FILTER_CHOICES_LIMIT + 1])
            if len(options_) <= FILTER_CHOICES_LIMIT:
                spec["choices"] = [
                    {"value": json_value(o.pk), "label": force_str(o)} for o in options_
                ]
            last = options.model
            for part in path.split("__")[:-1]:
                last = last._meta.get_field(part).related_model
            spec["null"] = bool(getattr(last._meta.get_field(path.split("__")[-1]), "null", False))
        elif finfo.choices:
            spec["choices"] = [{"value": v, "label": label} for v, label in finfo.choices]
        specs.append(spec)
    return specs


def _allowed_filter_paths(options: ModelOptions) -> set[str]:
    allowed = set(options.get_list_filter())
    allowed |= {c.path for c in list_columns(options) if c.path}
    allowed |= {c for c in available_columns(options) if c in options.info.fields}
    allowed.add(options.info.pk_name)
    allowed.add("pk")
    if options.date_hierarchy:
        allowed.add(options.date_hierarchy)
    hidden = set(options.exclude) | set(options.password_fields)
    return {p for p in allowed if p.split("__")[0] not in hidden}


def _split_lookup(model: type[models.Model], key: str, allowed: set[str]) -> tuple[str, str]:
    for lookup in sorted(FILTER_LOOKUPS, key=len, reverse=True):
        suffix = "__" + lookup
        if key.endswith(suffix) and key[: -len(suffix)] in allowed:
            return key[: -len(suffix)], lookup
    if key in allowed:
        return key, "exact"
    raise CrudError(400, _("Filtering on %(key)r is not allowed") % {"key": key})


def _parse_bool(raw: str) -> bool:
    low = raw.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise CrudError(400, _("Invalid boolean %(value)r") % {"value": raw})


def _convert(dfield: models.Field, lookup: str, raw: str) -> Any:
    if lookup == "isnull":
        return _parse_bool(raw)
    if lookup in ("year", "month", "day"):
        try:
            return int(raw)
        except ValueError:
            raise CrudError(400, _("Invalid number %(value)r") % {"value": raw}) from None
    if lookup.startswith("date"):
        try:
            return dt.date.fromisoformat(raw)
        except ValueError:
            raise CrudError(400, _("Invalid date %(value)r") % {"value": raw}) from None
    if lookup in ("icontains", "istartswith", "iexact"):
        return raw
    if lookup == "in":
        return [_convert(dfield, "exact", part) for part in raw.split(",") if part != ""]
    if isinstance(dfield, models.BooleanField):
        return _parse_bool(raw)
    try:
        return dfield.to_python(raw)
    except (ValidationError, ValueError, TypeError):
        raise CrudError(
            400, _("Invalid value %(value)r for %(field)s") % {"value": raw, "field": dfield.name}
        ) from None


def apply_filters(options: ModelOptions, qs: models.QuerySet, params: dict[str, str]):
    allowed = _allowed_filter_paths(options)
    q = Q()
    for key, raw in params.items():
        if key in RESERVED_PARAMS:
            continue
        path, lookup = _split_lookup(options.model, key, allowed)
        if path == "pk":
            path = options.info.pk_name
        dfield = django_field(options.model, path)
        q &= Q(**{f"{path}__{lookup}": _convert(dfield, lookup, raw)})
    return qs.filter(q).distinct() if _multiplies(options, params) else qs.filter(q)


def _multiplies(options: ModelOptions, params) -> bool:
    reg = get_registry()
    for key in params:
        if key in RESERVED_PARAMS:
            continue
        head = key.split("__")[0]
        rel = reg.get(options.label).relations.get(head)
        if rel is not None and rel.many:
            return True
    return False


# --- search ------------------------------------------------------------------------


def search_variants(term: str) -> set[str]:
    return {term, term.translate(_ARABIC_TO_PERSIAN), term.translate(_PERSIAN_TO_ARABIC)}


def apply_search(options: ModelOptions, qs: models.QuerySet, query: str) -> models.QuerySet:
    fields = options.get_search_fields()
    if not query.strip() or not fields:
        return qs
    lookups = []
    for f in fields:
        if f.startswith("^"):
            lookups.append(f"{f[1:]}__istartswith")
        elif f.startswith("="):
            lookups.append(f"{f[1:]}__iexact")
        elif f.startswith("@"):
            lookups.append(f"{f[1:]}__search")
        else:
            lookups.append(f"{f}__icontains")
    many = any(
        rel.many
        for f in fields
        if (rel := options.info.relations.get(f.lstrip("^=@").split("__")[0])) is not None
    )
    for term in query.split():
        term_q = Q()
        for variant in search_variants(term):
            for lookup in lookups:
                term_q |= Q(**{lookup: variant})
        qs = qs.filter(term_q)
    return qs.distinct() if many else qs


# --- ordering ----------------------------------------------------------------------


def sort_paths(options: ModelOptions, cols: list[Column] | None = None) -> dict[str, str]:
    """Column/field name → ORM path the list may be ordered by."""
    cols = cols if cols is not None else list_columns(options, available_columns(options))
    paths = {c.name: c.order_path for c in cols if c.sortable and c.order_path}
    if options.sortable_by is None:
        hidden = set(options.exclude) | set(options.password_fields)
        for f in options.info.fields.values():
            if f.kind != "m2m" and f.name not in hidden:
                paths.setdefault(f.name, f.name)
    else:
        paths = {k: v for k, v in paths.items() if k in options.sortable_by}
    return paths


def apply_ordering(options: ModelOptions, qs: models.QuerySet, ordering: str | None):
    paths = sort_paths(options)
    order: list[str] = []
    if ordering:
        for part in ordering.split(","):
            part = part.strip()
            if not part:
                continue
            name = part.lstrip("-")
            if name not in paths:
                raise CrudError(400, _("Ordering by %(field)r is not allowed") % {"field": part})
            path = paths[name]
            if part.startswith("-"):
                path = path[1:] if path.startswith("-") else f"-{path}"
            order.append(path)
    order = order or options.get_ordering()
    pk = options.info.pk_name
    if not any(o.lstrip("-") in (pk, "pk") for o in order):
        order.append(f"-{pk}")  # deterministic pagination
    return qs.order_by(*order)


def select_related_paths(options: ModelOptions, cols: list[Column]) -> list[str]:
    reg = get_registry()
    paths = set()
    for c in cols:
        if not c.path:
            continue
        steps, _ = reg.resolve_lookup(options.label, c.path)
        if steps and not any(s.many for s in steps):
            paths.add("__".join(s.relation for s in steps))
    return sorted(paths)


def apply_select_related(options: ModelOptions, qs: models.QuerySet, cols: list[Column]):
    related = options.list_select_related
    if related is True:
        return qs.select_related()
    if isinstance(related, list | tuple):
        return qs.select_related(*related) if related else qs
    paths = select_related_paths(options, cols)
    return qs.select_related(*paths) if paths else qs


# --- list --------------------------------------------------------------------------


def chosen_columns(options: ModelOptions, params: dict[str, str]) -> list[Column]:
    """``?columns=a,b`` (a saved view's columns), checked against what may be shown."""
    raw = params.get("columns")
    if not raw:
        return list_columns(options)
    allowed = set(available_columns(options))
    names = [n for n in raw.split(",") if n]
    bad = [n for n in names if n not in allowed]
    if bad:
        raise CrudError(400, _("Unknown column %(name)r") % {"name": bad[0]})
    return list_columns(options, names)


def list_queryset(options: ModelOptions, request, params: dict[str, str]) -> models.QuerySet:
    """The scoped, filtered, searched queryset of a list request (no ordering)."""
    from bi_admin.crud import hierarchy

    qs = model_queryset(options, request)
    qs = apply_filters(options, qs, params)
    qs = apply_search(options, qs, params.get("q", ""))
    return hierarchy.apply(options, request, qs, params)


def list_objects(options: ModelOptions, request, params: dict[str, str]) -> dict[str, Any]:
    from bi_admin.crud import hierarchy

    require(options, request, "view")
    cols = chosen_columns(options, params)
    qs = list_queryset(options, request, params)
    dh = hierarchy.describe(options, request, qs, params)
    qs = apply_ordering(options, qs, params.get("ordering"))
    qs = apply_select_related(options, qs, cols)

    try:
        page_size = int(params.get("page_size") or options.list_per_page)
        page_number = int(params.get("page") or 1)
    except ValueError:
        raise CrudError(400, _("page and page_size must be integers")) from None
    page_size = max(1, min(page_size, options.list_max_page_size))
    paginator = Paginator(qs, page_size)
    try:
        page = paginator.page(page_number)
    except EmptyPage:
        raise CrudError(404, _("Page %(page)s does not exist") % {"page": page_number}) from None
    filtered = params.get("q") or any(
        k not in RESERVED_PARAMS or k.startswith("dh_") for k in params
    )
    total: int | None = paginator.count
    if filtered:
        total = model_queryset(options, request).count() if options.show_full_result_count else None

    objs = list(page.object_list)
    rows = serialize_rows(options, request, cols, objs)
    editable = [c.name for c in cols if c.editable]
    if editable and options.has_change_permission(request):
        for row, obj in zip(rows, objs, strict=True):
            row["edit"] = {n: field_value(options, request, obj, n)[0] for n in editable}
    out = {
        "model": options.label,
        "columns": [c.as_dict() for c in cols],
        "rows": rows,
        "count": paginator.count,
        "total": total,
        "page": page.number,
        "page_size": page_size,
        "num_pages": paginator.num_pages,
    }
    if dh is not None:
        out["date_hierarchy"] = dh
    return out


def selection(options: ModelOptions, request, body: dict[str, Any]) -> models.QuerySet:
    """Rows a bulk operation applies to: ``{"pks": [...]}``, or ``{"all": true,
    "params": {...list query...}}`` for every row matching the list (capped)."""
    from bi_admin.conf import setting

    if body.get("all"):
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise CrudError(400, _("params must be an object"))
        qs = list_queryset(options, request, {k: str(v) for k, v in params.items()})
        limit = setting("BULK_EDIT_MAX_ROWS")
        if limit and qs.count() > limit:
            raise CrudError(
                400,
                _("Too many rows selected (the limit is %(limit)s).") % {"limit": limit},
                code="too_many_rows",
                limit=limit,
            )
        return qs
    pks = body.get("pks", [])
    if not isinstance(pks, list) or not pks:
        raise CrudError(400, _("Select at least one row"))
    pk_field = options.model._meta.pk
    try:
        values = [pk_field.to_python(pk) for pk in pks]
    except (ValidationError, ValueError, TypeError):
        raise CrudError(400, _("Invalid primary key in selection")) from None
    return model_queryset(options, request).filter(pk__in=values)


# --- detail ------------------------------------------------------------------------


def get_object(options: ModelOptions, request, pk: str) -> models.Model:
    pk_field = options.model._meta.pk
    try:
        value = pk_field.to_python(pk)
    except (ValidationError, ValueError):
        raise CrudError(404, _("Not found")) from None
    obj = model_queryset(options, request).filter(pk=value).first()
    if obj is None:
        raise CrudError(404, _("Not found"))
    return obj


REVERSE_LIMIT = 100  # linked objects listed for a reverse relation in ``fields``


def restricted_ref(obj: models.Model | None = None) -> dict[str, Any]:
    """Stands in for a related row outside the user's row-level scope: its pk (so a
    form can send it back unchanged) but never its label."""
    out: dict[str, Any] = {"pk": None, "label": _("Restricted"), "restricted": True}
    if obj is not None:
        out.update(pk=json_value(obj.pk), model=model_label(type(obj)))
    return out


def _scoped_related(request, manager) -> models.QuerySet:
    """``manager``'s rows that the user may see (the target's rules and get_queryset)."""
    visible = target_queryset(model_label(manager.model), request).values("pk")
    return manager.filter(pk__in=visible)


def field_value(options, request, obj: models.Model, name: str) -> tuple[Any, int]:
    """One form value of ``obj`` and how many linked rows are hidden from the user.

    Related rows outside the user's scope never show: M2M/reverse lists leave them
    out (and count them), a hidden FK target becomes a ``restricted`` ref."""
    if name in getattr(options, "password_fields", ()):
        return None, 0
    computed = options.computed_fields()
    if name in computed:
        try:
            value = computed[name](obj) if obj.pk is not None else None
        except Exception:
            logger.exception("Computed field %r of %s failed", name, model_label(type(obj)))
            value = None
        return json_value(value), 0
    try:
        f = options.model._meta.get_field(name)
    except FieldDoesNotExist:
        value = getattr(obj, name, None)
        return json_value(value() if callable(value) else value), 0
    if f.many_to_many or f.one_to_many:  # forward M2M, or a reverse relation
        if obj.pk is None:
            return [], 0
        manager = getattr(obj, f.name if f.concrete else f.get_accessor_name())
        linked = manager.all()
        shown = _scoped_related(request, linked)
        refs = [obj_ref(o) for o in (shown if f.concrete else shown[:REVERSE_LIMIT])]
        return refs, linked.count() - shown.count()
    if f.is_relation and f.concrete:
        if getattr(obj, f.attname, None) is None:
            return None, 0
        target = getattr(obj, name)
        visible = _scoped_related(request, type(target)._base_manager).filter(pk=target.pk)
        return (json_value(target) if visible.exists() else restricted_ref(target)), 0
    if f.is_relation:  # reverse one-to-one
        target = getattr(obj, f.get_accessor_name(), None) if obj.pk else None
        if target is None:
            return None, 0
        visible = _scoped_related(request, type(target)._base_manager)
        ok = visible.filter(pk=target.pk).exists()
        return (json_value(target) if ok else restricted_ref(target)), 0
    return json_value(f.value_from_object(obj)), 0


def serialize_object(options: ModelOptions, obj: models.Model, request=None) -> dict[str, Any]:
    """The detail payload: field values, inline rows, links and password status."""
    from bi_admin.crud import inlines as crud_inlines

    values: dict[str, Any] = {}
    hidden_links: dict[str, int] = {}
    for name in options.get_fields():
        values[name], hidden = field_value(options, request, obj, name)
        if hidden:
            hidden_links[name] = hidden
    out: dict[str, Any] = {"pk": json_value(obj.pk), "label": force_str(obj), "values": values}
    if hidden_links:
        out["hidden_links"] = hidden_links
    if request is not None and options.inlines:
        out["inlines"] = crud_inlines.serialize(options, request, obj)
    try:
        site_url = options.get_view_on_site_url(obj)
    except Exception:
        logger.exception("view_on_site of %s failed", options.label)
        site_url = None
    if site_url:
        out["site_url"] = site_url
    if options.change_password_form is not None and obj.pk is not None:
        out["password_status"] = password_status(obj)
    return out


def password_status(user) -> dict[str, Any]:
    """What the change form shows instead of a password hash (never the hash)."""
    from django.contrib.auth.hashers import identify_hasher

    usable = user.has_usable_password()
    algorithm = None
    if usable:
        try:
            algorithm = identify_hasher(user.password).algorithm
        except ValueError:
            algorithm = None
    return {"usable": usable, "algorithm": algorithm}


# --- create / update ---------------------------------------------------------------


def _initial_data(model: type[models.Model], fields: list[str], obj: models.Model):
    data = model_to_dict(obj, fields=fields)
    for name, value in list(data.items()):
        if isinstance(value, list):
            data[name] = [v.pk if isinstance(v, models.Model) else v for v in value]
        elif isinstance(value, FieldFile):
            data.pop(name)  # a file field reads uploads and the initial value, not data
    return data


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept ``{"pk": ..}`` refs for relations as well as bare pks."""
    out = {}
    for key, value in payload.items():
        if isinstance(value, dict) and "pk" in value:
            value = value["pk"]
        elif isinstance(value, list):
            value = [v["pk"] if isinstance(v, dict) and "pk" in v else v for v in value]
        out[key] = value
    return out


def snapshot(model: type[models.Model], names, obj: models.Model) -> dict[str, Any]:
    """Comparable values of the editable fields, to record what really changed."""
    out: dict[str, Any] = {}
    for name in names:
        try:
            f = model._meta.get_field(name)
        except FieldDoesNotExist:
            continue
        if f.many_to_many:
            out[name] = sorted(obj.pk and getattr(obj, name).values_list("pk", flat=True) or [])
        elif isinstance(f, models.FileField):
            out[name] = getattr(obj, name).name or ""
        else:
            out[name] = f.value_from_object(obj)
    return out


def diff_value(model: type[models.Model], name: str, value: Any) -> Any:
    """A history-diff value: relations as refs (label at the time), the rest as JSON."""
    f = model._meta.get_field(name)
    if f.many_to_many:
        objs = f.related_model._base_manager.filter(pk__in=value or [])
        return [obj_ref(o) for o in objs]
    if f.is_relation and value is not None:
        target = f.related_model._base_manager.filter(pk=value).first()
        return obj_ref(target) if target is not None else {"pk": json_value(value), "label": ""}
    value = json_value(value)
    if isinstance(value, str) and len(value) > DIFF_TEXT_LIMIT:
        value = value[:DIFF_TEXT_LIMIT] + "…"
    return value


def build_diff(model, before: dict, after: dict, changed: list[str], secret=()) -> dict:
    return {
        name: {
            "old": diff_value(model, name, before.get(name)),
            "new": diff_value(model, name, after[name]),
        }
        for name in changed
        if name in after and name not in secret
    }


def _keep_hidden_links(options, request, obj: models.Model, payload, names) -> None:
    """The form only shows the M2M links the user may see; a submitted list
    replaces those, and the hidden ones are added back so they're never lost."""
    for name in names:
        try:
            f = options.model._meta.get_field(name)
        except FieldDoesNotExist:
            continue
        if not f.many_to_many or name not in payload or not isinstance(payload[name], list):
            continue
        linked = getattr(obj, name).all()
        hidden_pks = set(linked.values_list("pk", flat=True)) - set(
            _scoped_related(request, linked).values_list("pk", flat=True)
        )
        submitted = [str(v) for v in payload[name]]
        payload[name] = payload[name] + [pk for pk in hidden_pks if str(pk) not in submitted]


def scope_relation_fields(form, request, obj: models.Model | None) -> None:
    """Relation fields may only point at rows the user may see — or keep the ones
    already linked, so editing other fields never fails on a hidden link."""
    for name, ffield in form.fields.items():
        qs = getattr(ffield, "queryset", None)
        if qs is None:
            continue
        try:
            mfield = form._meta.model._meta.get_field(name)
        except (FieldDoesNotExist, AttributeError):
            mfield = None
        if mfield is None or not mfield.is_relation or mfield.primary_key:
            continue
        q = model_filter(qs.model, request)
        if q is None:
            continue
        current: list[Any] = []
        if obj is not None and obj.pk is not None:
            if mfield.many_to_many:
                current = list(getattr(obj, name).values_list("pk", flat=True))
            elif getattr(obj, mfield.attname, None) is not None:
                current = [getattr(obj, mfield.attname)]
        ffield.queryset = qs.filter(q | Q(pk__in=current))


def form_errors(form) -> dict[str, list[str]]:
    return {field: [force_str(e) for e in errs] for field, errs in form.errors.items()}


def _save_as_source(options: ModelOptions, request, pk) -> models.Model:
    if not options.save_as:
        raise CrudError(400, _("Save as new is not enabled for this model"))
    return get_object(options, request, str(pk))


def save_object(
    options: ModelOptions,
    request,
    payload: dict[str, Any],
    obj: models.Model | None = None,
    partial: bool = False,
    files: MultiValueDict | None = None,
    fields: list[str] | None = None,
    log_extra: dict | None = None,
) -> models.Model:
    """Validate and save one object with its inline rows, all or nothing.

    ``fields`` limits what may be changed (list editing, bulk edit, import);
    ``files`` holds uploads (see ``crud.forms``)."""
    from bi_admin.crud import inlines as crud_inlines

    change = obj is not None
    require(options, request, "change" if change else "add", obj)
    if not isinstance(payload, dict):
        raise CrudError(400, _("Request body must be a JSON object"))
    files = files if files is not None else MultiValueDict()
    payload = dict(payload)
    inline_payload = payload.pop(crud_forms.INLINES_KEY, None)
    save_as_pk = payload.pop(crud_forms.SAVE_AS_KEY, None)
    source = _save_as_source(options, request, save_as_pk) if save_as_pk and not change else None
    if fields is not None:
        inline_payload = None
    # The add form (two password fields for users) when the payload uses its own
    # fields; API clients may still send the regular field set (``password_fields``).
    add_only = set(options.get_add_fields()) - set(options.info.fields)
    use_add_form = (
        not change
        and options.add_form is not None
        and fields is None
        and bool(set(payload) & add_only)
        and set(payload) <= set(options.get_add_fields())
    )
    if use_add_form:
        allowed = set(options.get_add_fields()) - set(options.get_readonly_fields())
        form_fields = [n for n in options.get_add_fields() if n in options.info.fields]
    else:
        form_fields = options.get_form_fields()
        allowed = set(form_fields) | set(options.password_fields)
    if fields is not None:
        allowed &= set(fields)
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise CrudError(
            400,
            _("Unknown or read-only fields"),
            errors={k: [_("This field cannot be edited.")] for k in unknown},
        )
    file_names = {f.name for f in crud_forms.file_fields(options.model, form_fields)}
    file_values = {n: payload.pop(n) for n in list(payload) if n in file_names}
    payload = normalize_payload(payload)

    passwords = {p: payload.pop(p) for p in options.password_fields if p in payload}
    if change:
        _keep_hidden_links(options, request, obj, payload, form_fields)
    before = snapshot(options.model, form_fields, obj) if change else {}
    data = _initial_data(options.model, form_fields, obj) if (change and partial) else {}
    data.update(payload)
    errors: dict[str, list[str]] = {}
    form_files = crud_forms.apply_files(
        options.model, form_fields, file_values, data, files, lambda n: n, errors, instance=obj
    )
    initial = _copied_files(options.model, form_fields, file_values, source)
    if use_add_form:
        form = options.add_form(data=data, files=form_files)
    else:
        form_class = crud_forms.model_form_class(options.model, form_fields, options.form)
        form = form_class(data=data, files=form_files, instance=obj, initial=initial)
    scope_relation_fields(form, request, obj)
    if not form.is_valid():
        for name, msgs in form_errors(form).items():
            errors.setdefault(name, []).extend(msgs)
    for name, raw in passwords.items():
        if raw in (None, ""):
            continue
        try:
            password_validation.validate_password(raw, form.instance)
        except ValidationError as exc:
            errors.setdefault(name, []).extend(force_str(m) for m in exc.messages)
    if not errors:
        for name, msgs in options.validate_save(request, form, obj).items():
            errors.setdefault(name, []).extend(msgs)

    submissions = []
    inline_errors: dict[str, Any] = {}
    if inline_payload is not None:
        submissions, inline_errors = crud_inlines.build_all(
            options, request, form.instance, inline_payload, files, source
        )
    if errors or inline_errors:
        extra: dict[str, Any] = {"errors": errors}
        if inline_errors:
            extra["inline_errors"] = inline_errors
        raise CrudError(400, _("Validation failed"), **extra)

    with transaction.atomic(using=router.db_for_write(options.model)):
        instance = form.save(commit=False)
        for raw in passwords.values():
            if raw not in (None, ""):
                instance.set_password(raw)
        if not change:
            for name in options.password_fields:
                if (
                    not use_add_form
                    and passwords.get(name) in (None, "")
                    and hasattr(instance, "set_unusable_password")
                ):
                    instance.set_unusable_password()
        options.save_model(request, instance, form, change)
        form.save_m2m()
        inline_log = crud_inlines.save_all(options, request, form, submissions, change)
        if not model_queryset(options, request).filter(pk=instance.pk).exists():
            # rolls the save back: users can't create rows (or move them) out of scope
            raise CrudError(
                403,
                _("You can't save a record outside your access scope."),
                code="out_of_scope",
            )
        after = snapshot(options.model, form_fields, instance)
        changed = [n for n in after if before.get(n) != after[n]] if change else []
        changed += [n for n, raw in passwords.items() if raw]
        if change and not changed and not inline_log:
            instance._gozar_unchanged = True  # nothing to log (import reports a skip)
            return instance
        changes: dict[str, Any] = dict(log_extra or {})
        if change:
            changes["fields"] = changed
            diff = build_diff(options.model, before, after, changed, options.password_fields)
            if diff:
                changes["diff"] = diff
        if inline_log:
            changes["inlines"] = inline_log
        if source is not None:
            changes["copied_from"] = force_str(source.pk)
        log_change(
            request,
            instance,
            ChangeLog.Action.CHANGE if change else ChangeLog.Action.ADD,
            changes,
        )
    return instance


def _copied_files(model, names, raw_files: dict, source) -> dict[str, Any]:
    """ "Save as new" keeps the copied object's files: a file value sent back as
    ``{"name": ...}`` is reused only when it is the source object's own file."""
    if source is None:
        return {}
    out = {}
    for f in crud_forms.file_fields(model, names):
        sent = raw_files.get(f.name)
        current = getattr(source, f.name)
        if isinstance(sent, dict) and current and sent.get("name") == current.name:
            out[f.name] = current.name
    return out


# --- delete ------------------------------------------------------------------------


def _collect(objs, model):
    collector = Collector(using=router.db_for_write(model))
    protected: list[models.Model] = []
    try:
        collector.collect(objs)
    except ProtectedError as exc:
        protected = list(exc.protected_objects)
    except RestrictedError as exc:
        protected = list(exc.restricted_objects)
    return collector, protected


def deletion_plan(request, objs: list[models.Model], model: type[models.Model]) -> dict[str, Any]:
    collector, protected = _collect(objs, model)
    counts: dict[type[models.Model], int] = {}
    for m, instances in collector.data.items():
        counts[m] = counts.get(m, 0) + len(instances)
    for qs in collector.fast_deletes:
        counts[qs.model] = counts.get(qs.model, 0) + qs.count()
    reg = get_registry()
    summary, perms_needed, out_of_scope = [], [], []
    for m, n in counts.items():
        if not n:
            continue
        label = model_label(m)
        summary.append(
            {"model": label, "verbose_name": force_str(m._meta.verbose_name_plural), "count": n}
        )
        if label in reg and not site.options_for(label).has_delete_permission(request):
            perms_needed.append(force_str(m._meta.verbose_name_plural))
        hidden = _out_of_scope(request, m, collector)
        if hidden:
            out_of_scope.append(
                {
                    "model": label,
                    "verbose_name": force_str(m._meta.verbose_name_plural),
                    "count": hidden,
                }
            )
    by_model: dict[str, dict[str, Any]] = {}
    for p in protected:
        entry = by_model.setdefault(
            model_label(type(p)),
            {
                "model": model_label(type(p)),
                "verbose_name": force_str(type(p)._meta.verbose_name_plural),
                "count": 0,
                "sample": [],
            },
        )
        entry["count"] += 1
        if len(entry["sample"]) < 5 and _in_scope(request, p):
            entry["sample"].append(obj_ref(p))  # never name rows the user can't see
    return {
        "deletable": not protected and not perms_needed and not out_of_scope,
        "summary": sorted(summary, key=lambda s: -s["count"]),
        "protected": list(by_model.values()),
        "perms_needed": perms_needed,
        "out_of_scope": out_of_scope,
    }


def _out_of_scope(request, model: type[models.Model], collector: Collector) -> int:
    """How many rows of ``model`` the deletion would cascade to outside the user's
    row-level scope (such a deletion is refused)."""
    q = model_filter(model, request)
    if q is None:
        return 0
    visible = model._base_manager.filter(q).values("pk")
    pks = {obj.pk for obj in collector.data.get(model, ())}
    hidden = len(pks) - model._base_manager.filter(q, pk__in=pks).count() if pks else 0
    for qs in collector.fast_deletes:
        if qs.model is model:
            hidden += qs.exclude(pk__in=visible).count()
    return hidden


def _in_scope(request, obj: models.Model) -> bool:
    q = model_filter(type(obj), request)
    return q is None or type(obj)._base_manager.filter(q, pk=obj.pk).exists()


def delete_objects(options: ModelOptions, request, objs: list[models.Model]) -> dict[str, Any]:
    require(options, request, "delete")
    for obj in objs:
        require(options, request, "delete", obj)
    if not objs:
        return {"deleted": 0}
    plan = deletion_plan(request, objs, options.model)
    if not plan["deletable"]:
        raise CrudError(409, _("Cannot delete"), plan=plan)
    with transaction.atomic(using=router.db_for_write(options.model)):
        for obj in objs:
            log_change(request, obj, ChangeLog.Action.DELETE, {})
            options.delete_model(request, obj)
    return {"deleted": len(objs), "summary": plan["summary"]}


def delete_queryset(request, queryset: models.QuerySet) -> dict[str, Any]:
    options = site.options_for(model_label(queryset.model))
    return delete_objects(options, request, list(queryset))


# --- actions -----------------------------------------------------------------------


def run_action(options: ModelOptions, request, name: str, body: dict[str, Any]) -> dict[str, Any]:
    actions = options.get_actions()
    if name not in actions:
        raise CrudError(404, _("Unknown action %(name)r") % {"name": name})
    act = actions[name]
    require(options, request, act.permission)
    qs = selection(options, request, body)
    result = act.handler(request, qs) or {}
    cache.bump(options.model)  # actions often use QuerySet.update(), which sends no signals
    return {"action": name, **result}


def action_plan(options: ModelOptions, request, body: dict[str, Any]) -> dict[str, Any]:
    require(options, request, "delete")
    objs = list(selection(options, request, body))
    return deletion_plan(request, objs, options.model)


# --- autocomplete ------------------------------------------------------------------


def autocomplete(
    options: ModelOptions,
    request,
    field_name: str,
    query: str,
    inline: str | None = None,
    limit: int = AUTOCOMPLETE_LIMIT,
) -> dict[str, Any]:
    from bi_admin.crud import inlines as crud_inlines

    if not (options.has_add_permission(request) or options.has_change_permission(request)):
        if not options.has_view_permission(request):
            raise CrudError(403, _("Permission denied"))
    limit = max(1, min(int(limit), AUTOCOMPLETE_MAX_LIMIT))
    if inline:
        target_label = crud_inlines.relation_target(options, request, inline, field_name)
    else:
        target_label = _relation_target(options, field_name)
    qs = target_queryset(target_label, request)
    if target_label in get_registry():
        target_opts = site.options_for(target_label)
        qs = apply_search(target_opts, qs, query)
        qs = apply_ordering(target_opts, qs, None)
    elif query.strip():
        qs = _search_unregistered(qs, query)
    results = [{"pk": json_value(o.pk), "label": force_str(o)} for o in qs[: limit + 1]]
    return {"results": results[:limit], "more": len(results) > limit}


def _relation_target(options: ModelOptions, field_name: str) -> str:
    fields = set(options.get_fields()) | set(options.get_list_filter())
    fields |= set(options.get_add_fields()) | set(options.get_bulk_edit_fields())
    if field_name not in fields:
        raise CrudError(
            400,
            _("%(field)r is not a relation field of %(model)s")
            % {"field": field_name, "model": options.label},
        )
    try:
        steps, finfo = get_registry().resolve_lookup(options.label, field_name)
    except LookupError:
        raise CrudError(400, _("Unknown field %(field)r") % {"field": field_name}) from None
    if finfo.is_relation:
        return finfo.related_model
    if ends_on_relation(steps, field_name):
        return steps[-1].target
    raise CrudError(400, _("%(field)r is not a relation field") % {"field": field_name})


def _search_unregistered(qs: models.QuerySet, query: str) -> models.QuerySet:
    """Search a model outside the registry by its text fields (e.g. permissions)."""
    names = [
        f.name
        for f in qs.model._meta.concrete_fields
        if isinstance(f, models.CharField) and not f.choices
    ]
    for term in query.split():
        term_q = Q()
        for variant in search_variants(term):
            for name in names:
                term_q |= Q(**{f"{name}__icontains": variant})
        qs = qs.filter(term_q)
    return qs


# --- history -----------------------------------------------------------------------


def log_change(request, obj: models.Model, action: str, changes: dict) -> None:
    user = getattr(request, "user", None)
    ChangeLog.objects.create(
        user=user if user is not None and user.is_authenticated else None,
        model_label=model_label(type(obj)),
        object_pk=force_str(obj.pk),
        object_repr=force_str(obj)[:255],
        action=action,
        changes=changes,
    )


def _visible_refs(request, refs: list[dict]) -> dict[tuple[str, str], bool]:
    """Which ``{"model", "pk"}`` refs the user may still see (one query per model)."""
    by_model: dict[str, set] = {}
    for ref in refs:
        if isinstance(ref, dict) and ref.get("model"):
            by_model.setdefault(ref["model"], set()).add(ref.get("pk"))
    out = {}
    for label, pks in by_model.items():
        try:
            visible = {
                str(pk)
                for pk in target_queryset(label, request)
                .filter(pk__in=[p for p in pks if p is not None])
                .values_list("pk", flat=True)
            }
        except (LookupError, ValueError, ValidationError):
            visible = set()
        for pk in pks:
            out[(label, str(pk))] = str(pk) in visible
    return out


def _diff_display(model, request, diff: dict, visible: dict) -> list[dict[str, Any]]:
    out = []
    for name, change in diff.items():
        try:
            f = model._meta.get_field(name)
        except (FieldDoesNotExist, AttributeError):
            f = None
        label = capfirst(force_str(getattr(f, "verbose_name", name)))
        kind = _field_kind(f)
        out.append(
            {
                "field": name,
                "label": label,
                "kind": kind,
                "old": _shown(f, change.get("old"), visible),
                "new": _shown(f, change.get("new"), visible),
            }
        )
    return out


def _field_kind(f) -> str:
    if f is None:
        return "other"
    from bi_admin.discovery.registry import _INTERNAL_KIND

    return _INTERNAL_KIND.get(f.get_internal_type(), "other")


def _shown(f, value, visible):
    if isinstance(value, list):
        return [_shown(f, v, visible) for v in value]
    if isinstance(value, dict) and value.get("model"):
        if not visible.get((value["model"], str(value.get("pk"))), False):
            return restricted_ref()
        return value
    if f is not None and f.choices and value is not None:
        return force_str(dict(f.flatchoices).get(value, value))
    return value


def _refs_in(diff: dict) -> list[dict]:
    refs = []
    for change in diff.values():
        for v in (change.get("old"), change.get("new")):
            refs += v if isinstance(v, list) else [v]
    return [r for r in refs if isinstance(r, dict)]


def history(options: ModelOptions, request, pk: str) -> list[dict[str, Any]]:
    """The object's change log with field-level diffs. Related rows named in a diff
    that the user can't see (now) show as ``restricted``."""
    require(options, request, "view")
    entries = list(
        ChangeLog.objects.filter(model_label=options.label, object_pk=pk).select_related("user")[
            :200
        ]
    )
    refs = []
    for e in entries:
        refs += _refs_in((e.changes or {}).get("diff") or {})
        for inline in (e.changes or {}).get("inlines") or []:
            for row in inline.get("changed", []):
                refs += _refs_in(row.get("diff") or {})
    visible = _visible_refs(request, refs)
    out = []
    for e in entries:
        changes = dict(e.changes or {})
        item = {
            "timestamp": e.timestamp.isoformat(),
            "user": force_str(e.user) if e.user else None,
            "action": e.action,
            "action_display": e.get_action_display(),
            "changes": {"fields": changes.get("fields", [])},
            "diff": _diff_display(options.model, request, changes.get("diff") or {}, visible),
        }
        if changes.get("inlines"):
            item["inlines"] = _inline_history(changes["inlines"], request, visible)
        if changes.get("source"):
            item["source"] = changes["source"]
        out.append(item)
    return out


def _inline_history(inlines: list[dict], request, visible) -> list[dict]:
    from django.apps import apps

    out = []
    for entry in inlines:
        try:
            model = apps.get_model(entry["model"])
        except (LookupError, KeyError, ValueError):
            model = None
        out.append(
            {
                **entry,
                "changed": [
                    {
                        **row,
                        "diff": _diff_display(model, request, row.get("diff") or {}, visible)
                        if model
                        else [],
                    }
                    for row in entry.get("changed", [])
                ],
            }
        )
    return out


def recent_actions(request, limit: int = 10) -> list[dict[str, Any]]:
    """The user's own latest edits (admin's "Recent actions"), for models they can
    still view; rows that are now outside their scope are left out."""
    visible_labels = {o.label: o for o in visible_models(request)}
    entries = [
        e
        for e in ChangeLog.objects.filter(user=request.user)[: limit * 5]
        if e.model_label in visible_labels
    ]
    by_model: dict[str, set] = {}
    for e in entries:
        if e.action != ChangeLog.Action.DELETE:
            by_model.setdefault(e.model_label, set()).add(e.object_pk)
    alive: dict[str, set] = {}
    for label, pks in by_model.items():
        opts = visible_labels[label]
        pk_field = opts.model._meta.pk
        values = []
        for pk in pks:
            try:
                values.append(pk_field.to_python(pk))
            except (ValidationError, ValueError):
                continue
        alive[label] = {
            force_str(pk)
            for pk in model_queryset(opts, request)
            .filter(pk__in=values)
            .values_list("pk", flat=True)
        }
    out = []
    for e in entries:
        exists = e.action != ChangeLog.Action.DELETE and e.object_pk in alive.get(e.model_label, ())
        if e.action != ChangeLog.Action.DELETE and not exists:
            continue
        opts = visible_labels[e.model_label]
        out.append(
            {
                "timestamp": e.timestamp.isoformat(),
                "action": e.action,
                "action_display": e.get_action_display(),
                "model": e.model_label,
                "verbose_name": capfirst(opts.info.verbose_name),
                "object_pk": e.object_pk,
                "object_repr": e.object_repr,
                "exists": exists,
            }
        )
        if len(out) >= limit:
            break
    return out
