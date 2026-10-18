"""CRUD service layer: listing, filtering, serialization, validation, deletion.

The API layer stays thin; everything that touches the ORM lives here so the
same rules apply to every caller (JSON API today, report drill-down later).
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from dataclasses import dataclass
from typing import Any

from django.contrib.auth import password_validation
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.paginator import EmptyPage, Paginator
from django.db import models, router, transaction
from django.db.models import Q
from django.db.models.deletion import Collector, ProtectedError, RestrictedError
from django.db.models.fields.files import FieldFile
from django.forms.models import model_to_dict, modelform_factory
from django.utils.encoding import force_str
from django.utils.text import capfirst
from django.utils.translation import gettext as _

from bi_admin.crud.options import ModelOptions, site
from bi_admin.discovery import get_registry, model_label
from bi_admin.models import ChangeLog

RESERVED_PARAMS = {"q", "page", "page_size", "ordering"}
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
}
AUTOCOMPLETE_LIMIT = 20
FILTER_CHOICES_LIMIT = 100

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


def require(options: ModelOptions, request, perm: str) -> None:
    if not options.has_perm(request, perm):
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
        try:
            return {"name": value.name, "url": value.url}
        except ValueError:
            return {"name": value.name, "url": None}
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "sortable": self.sortable,
            "is_link": self.is_link,
        }


def list_columns(options: ModelOptions) -> list[Column]:
    reg = get_registry()
    links = set(options.get_list_display_links())
    cols = []
    for name in options.get_list_display():
        if name == "__str__":
            cols.append(
                Column(
                    name,
                    capfirst(options.info.verbose_name),
                    "string",
                    False,
                    None,
                    is_link=name in links,
                )
            )
            continue
        try:
            steps, finfo = reg.resolve_lookup(options.label, name)
        except LookupError:
            fn = getattr(options, name, None) or getattr(options.model, name, None)
            if fn is None:
                raise CrudError(
                    500,
                    _("list_display entry %(name)r is not a field or callable") % {"name": name},
                ) from None
            label = getattr(fn, "short_description", None) or name.replace("_", " ")
            cols.append(Column(name, force_str(label), "other", False, None, is_link=name in links))
            continue
        label = path_label(options.model, name)
        kind = "fk" if ends_on_relation(steps, name) else finfo.kind
        choices = dict(finfo.choices) if finfo.choices else None
        cols.append(
            Column(
                name,
                label,
                kind,
                not any(s.many for s in steps),
                name,
                choices,
                is_link=name in links,
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
        fn = getattr(options, col.name, None)
        if callable(fn):
            return json_value(fn(obj))
        value = getattr(obj, col.name)
        return json_value(value() if callable(value) else value)
    value = _walk(obj, col.path)
    if col.choices is not None and value is not None:
        return force_str(col.choices.get(value, value))
    return json_value(value)


# --- filters -----------------------------------------------------------------------


def filter_specs(options: ModelOptions) -> list[dict[str, Any]]:
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
            qs = reg.get(target).model._default_manager.all()
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
    allowed.add(options.info.pk_name)
    allowed.add("pk")
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
    if lookup in ("year", "month"):
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


def apply_ordering(options: ModelOptions, qs: models.QuerySet, ordering: str | None):
    sortable = {c.path for c in list_columns(options) if c.sortable and c.path}
    hidden = set(options.exclude) | set(options.password_fields)
    sortable |= {
        f.name for f in options.info.fields.values() if f.kind != "m2m" and f.name not in hidden
    }
    order: list[str] = []
    if ordering:
        for part in ordering.split(","):
            part = part.strip()
            if not part:
                continue
            if part.lstrip("-") not in sortable:
                raise CrudError(400, _("Ordering by %(field)r is not allowed") % {"field": part})
            order.append(part)
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


# --- list --------------------------------------------------------------------------


def list_objects(options: ModelOptions, request, params: dict[str, str]) -> dict[str, Any]:
    require(options, request, "view")
    cols = list_columns(options)
    qs = options.get_queryset(request)
    total_unfiltered = None
    qs = apply_filters(options, qs, params)
    qs = apply_search(options, qs, params.get("q", ""))
    qs = apply_ordering(options, qs, params.get("ordering"))
    related = select_related_paths(options, cols)
    if related:
        qs = qs.select_related(*related)

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
    if params.get("q") or any(k not in RESERVED_PARAMS for k in params):
        total_unfiltered = options.get_queryset(request).count()

    rows = [
        {
            "pk": json_value(obj.pk),
            "label": force_str(obj),
            "cells": [cell_value(options, c, obj) for c in cols],
        }
        for obj in page.object_list
    ]
    return {
        "model": options.label,
        "columns": [c.as_dict() for c in cols],
        "rows": rows,
        "count": paginator.count,
        "total": total_unfiltered if total_unfiltered is not None else paginator.count,
        "page": page.number,
        "page_size": page_size,
        "num_pages": paginator.num_pages,
    }


# --- detail ------------------------------------------------------------------------


def get_object(options: ModelOptions, request, pk: str) -> models.Model:
    pk_field = options.model._meta.pk
    try:
        value = pk_field.to_python(pk)
    except (ValidationError, ValueError):
        raise CrudError(404, _("Not found")) from None
    obj = options.get_queryset(request).filter(pk=value).first()
    if obj is None:
        raise CrudError(404, _("Not found"))
    return obj


def serialize_object(options: ModelOptions, obj: models.Model) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in options.get_fields():
        if name in options.password_fields:
            values[name] = None
            continue
        try:
            f = options.model._meta.get_field(name)
        except FieldDoesNotExist:
            value = getattr(obj, name, None)
            values[name] = json_value(value() if callable(value) else value)
            continue
        if f.many_to_many:
            values[name] = [obj_ref(o) for o in getattr(obj, name).all()] if obj.pk else []
        elif f.is_relation:
            has_value = getattr(obj, f.attname, None) is not None
            values[name] = json_value(getattr(obj, name)) if has_value else None
        else:
            values[name] = json_value(f.value_from_object(obj))
    return {"pk": json_value(obj.pk), "label": force_str(obj), "values": values}


# --- create / update ---------------------------------------------------------------


def _form_class(options: ModelOptions):
    return modelform_factory(options.model, fields=options.get_form_fields())


def _initial_data(options: ModelOptions, obj: models.Model) -> dict[str, Any]:
    data = model_to_dict(obj, fields=options.get_form_fields())
    for name, value in list(data.items()):
        if isinstance(value, list):
            data[name] = [v.pk if isinstance(v, models.Model) else v for v in value]
    return data


def _normalize_payload(options: ModelOptions, payload: dict[str, Any]) -> dict[str, Any]:
    """Accept ``{"pk": ..}`` refs for relations as well as bare pks."""
    out = {}
    for key, value in payload.items():
        if isinstance(value, dict) and "pk" in value:
            value = value["pk"]
        elif isinstance(value, list):
            value = [v["pk"] if isinstance(v, dict) and "pk" in v else v for v in value]
        out[key] = value
    return out


def _snapshot(options: ModelOptions, obj: models.Model) -> dict[str, Any]:
    """Comparable values of the editable fields, to record what really changed."""
    out: dict[str, Any] = {}
    for name in options.get_form_fields():
        f = options.model._meta.get_field(name)
        if f.many_to_many:
            out[name] = sorted(obj.pk and getattr(obj, name).values_list("pk", flat=True) or [])
        else:
            out[name] = f.value_from_object(obj)
    return out


def _form_errors(form) -> dict[str, list[str]]:
    return {
        (field if field != "__all__" else "__all__"): [force_str(e) for e in errs]
        for field, errs in form.errors.items()
    }


def save_object(
    options: ModelOptions,
    request,
    payload: dict[str, Any],
    obj: models.Model | None = None,
    partial: bool = False,
) -> models.Model:
    change = obj is not None
    require(options, request, "change" if change else "add")
    if not isinstance(payload, dict):
        raise CrudError(400, _("Request body must be a JSON object"))
    payload = _normalize_payload(options, payload)
    allowed = set(options.get_form_fields()) | set(options.password_fields)
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise CrudError(
            400,
            _("Unknown or read-only fields"),
            errors={k: [_("This field cannot be edited.")] for k in unknown},
        )

    passwords = {p: payload.pop(p) for p in options.password_fields if p in payload}
    before = _snapshot(options, obj) if change else {}
    data = _initial_data(options, obj) if (change and partial) else {}
    data.update(payload)
    form = _form_class(options)(data=data, instance=obj)
    valid = form.is_valid()
    errors = _form_errors(form) if not valid else {}

    for name, raw in passwords.items():
        if raw in (None, ""):
            continue
        try:
            password_validation.validate_password(raw, form.instance)
        except ValidationError as exc:
            errors.setdefault(name, []).extend(force_str(m) for m in exc.messages)
    if errors:
        raise CrudError(400, _("Validation failed"), errors=errors)

    with transaction.atomic(using=router.db_for_write(options.model)):
        instance = form.save(commit=False)
        for raw in passwords.values():
            if raw not in (None, ""):
                instance.set_password(raw)
        if not change:
            for name in options.password_fields:
                if passwords.get(name) in (None, "") and hasattr(instance, "set_unusable_password"):
                    instance.set_unusable_password()
        options.save_model(request, instance, form, change)
        form.save_m2m()
        after = _snapshot(options, instance)
        changed = [n for n in after if before.get(n) != after[n]]
        changed += [n for n, raw in passwords.items() if raw]
        if change and not changed:
            return instance
        log_change(
            request,
            instance,
            ChangeLog.Action.CHANGE if change else ChangeLog.Action.ADD,
            {"fields": changed} if change else {},
        )
    return instance


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
    summary, perms_needed = [], []
    for m, n in counts.items():
        if not n:
            continue
        label = model_label(m)
        summary.append(
            {"model": label, "verbose_name": force_str(m._meta.verbose_name_plural), "count": n}
        )
        if label in reg and not site.options_for(label).has_delete_permission(request):
            perms_needed.append(force_str(m._meta.verbose_name_plural))
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
        if len(entry["sample"]) < 5:
            entry["sample"].append(obj_ref(p))
    return {
        "deletable": not protected and not perms_needed,
        "summary": sorted(summary, key=lambda s: -s["count"]),
        "protected": list(by_model.values()),
        "perms_needed": perms_needed,
    }


def delete_objects(options: ModelOptions, request, objs: list[models.Model]) -> dict[str, Any]:
    require(options, request, "delete")
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


def run_action(options: ModelOptions, request, name: str, pks: list[Any]) -> dict[str, Any]:
    actions = options.get_actions()
    if name not in actions:
        raise CrudError(404, _("Unknown action %(name)r") % {"name": name})
    act = actions[name]
    require(options, request, act.permission)
    if not isinstance(pks, list) or not pks:
        raise CrudError(400, _("Select at least one row"))
    pk_field = options.model._meta.pk
    try:
        values = [pk_field.to_python(pk) for pk in pks]
    except (ValidationError, ValueError, TypeError):
        raise CrudError(400, _("Invalid primary key in selection")) from None
    qs = options.get_queryset(request).filter(pk__in=values)
    result = act.handler(request, qs) or {}
    return {"action": name, **result}


def action_plan(options: ModelOptions, request, pks: list[Any]) -> dict[str, Any]:
    require(options, request, "delete")
    pk_field = options.model._meta.pk
    try:
        values = [pk_field.to_python(pk) for pk in pks]
    except (ValidationError, ValueError, TypeError):
        raise CrudError(400, _("Invalid primary key in selection")) from None
    objs = list(options.get_queryset(request).filter(pk__in=values))
    return deletion_plan(request, objs, options.model)


# --- autocomplete ------------------------------------------------------------------


def autocomplete(options: ModelOptions, request, field_name: str, query: str) -> dict[str, Any]:
    if not (options.has_add_permission(request) or options.has_change_permission(request)):
        if not options.has_view_permission(request):
            raise CrudError(403, _("Permission denied"))
    if field_name not in set(options.get_fields()) | set(options.get_list_filter()):
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
        target_label = finfo.related_model
    elif ends_on_relation(steps, field_name):
        target_label = steps[-1].target
    else:
        raise CrudError(400, _("%(field)r is not a relation field") % {"field": field_name})
    target = get_registry().get(target_label).model if target_label in get_registry() else None
    if target is None:
        from django.apps import apps as django_apps

        target = django_apps.get_model(target_label)
    if target_label in get_registry():
        target_opts = site.options_for(target_label)
        qs = target_opts.get_queryset(request)
        qs = apply_search(target_opts, qs, query)
        qs = apply_ordering(target_opts, qs, None)
    else:
        qs = target._default_manager.all()
    results = [
        {"pk": json_value(o.pk), "label": force_str(o)} for o in qs[: AUTOCOMPLETE_LIMIT + 1]
    ]
    return {"results": results[:AUTOCOMPLETE_LIMIT], "more": len(results) > AUTOCOMPLETE_LIMIT}


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


def history(options: ModelOptions, request, pk: str) -> list[dict[str, Any]]:
    require(options, request, "view")
    entries = ChangeLog.objects.filter(model_label=options.label, object_pk=pk).select_related(
        "user"
    )[:200]
    return [
        {
            "timestamp": e.timestamp.isoformat(),
            "user": force_str(e.user) if e.user else None,
            "action": e.action,
            "action_display": e.get_action_display(),
            "changes": e.changes,
        }
        for e in entries
    ]
