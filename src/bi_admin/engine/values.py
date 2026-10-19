"""Distinct values of a dimension, for filter value pickers in the report builder."""

from __future__ import annotations

from typing import Any

from django.db.models import F, Q
from django.utils.encoding import force_str
from django.utils.translation import gettext as _

from bi_admin.access.rules import semantic_queryset, target_queryset
from bi_admin.engine.runner import authorize
from bi_admin.engine.spec import ReportSpec
from bi_admin.engine.validator import SpecError, SpecValidationError
from bi_admin.execution.limits import statement_timeout
from bi_admin.execution.router import reporting, reports_database
from bi_admin.semantic import semantic_site
from bi_admin.semantic.base import active_language

LIMIT = 50


def dimension_values(semantic_model: str, key: str, query: str = "", request=None) -> dict:
    """Up to ``LIMIT`` values of dimension ``key`` matching ``query``.

    * choice fields → their choices (labels in the active language);
    * paths ending on a relation → the related objects, searched like the CRUD
      autocomplete (the target's ``search_fields``, Arabic/Persian letters);
    * other category/geo/number dimensions → distinct values from the semantic
      model's queryset (``icontains`` for text).

    Only rows inside the user's row-level scope are listed, and dimensions they
    may not see are unknown.
    """
    from bi_admin.crud import service

    authorize(ReportSpec(semantic_model=semantic_model, measures=[]), request)
    sem = semantic_site.get(semantic_model)
    visible = sem.visible_dimensions(request)
    dim = visible.get(key)
    if dim is None or dim.type == "time":
        raise SpecValidationError(
            [
                SpecError(
                    "unknown_dimension",
                    _("Unknown dimension %(key)r on %(model)s") % {"key": key, "model": sem.name},
                    "dimension",
                    key,
                    [k for k, d in visible.items() if d.type != "time"],
                )
            ]
        )
    terms = query.split()
    if dim.choices:
        lang = active_language()

        def matches(label: str, value: Any) -> bool:
            haystack = f"{label} {value}".lower()
            return all(
                any(v.lower() in haystack for v in service.search_variants(t)) for t in terms
            )

        items = [
            {"value": v, "label": labels[lang]}
            for v, labels in dim.choices
            if matches(labels[lang], v)
        ]
        return {"values": items[:LIMIT], "more": len(items) > LIMIT}

    ends_on_relation = dim.path is not None and len(dim.steps) == len(dim.path.split("__"))
    if dim.field is not None and (dim.field.is_relation or ends_on_relation):
        target = dim.field.related_model if dim.field.is_relation else dim.steps[-1].target
        options = service.site.options_for(target)
        qs = service.apply_search(options, target_queryset(target, request), query)
        qs = service.apply_ordering(options, qs, None)
        objs = _fetch(qs[: LIMIT + 1])
        return {
            "values": [{"value": service.json_value(o.pk), "label": force_str(o)} for o in objs][
                :LIMIT
            ],
            "more": len(objs) > LIMIT,
        }

    qs = semantic_queryset(sem, request).annotate(_gv=dim.expr if dim.path is None else F(dim.path))
    qs = qs.filter(_gv__isnull=False)
    if dim.type in ("category", "geo"):
        for term in terms:
            term_q = Q()
            for variant in service.search_variants(term):
                term_q |= Q(_gv__icontains=variant)
            qs = qs.filter(term_q)
    values = _fetch(qs.order_by("_gv").values_list("_gv", flat=True).distinct()[: LIMIT + 1])
    items: list[dict[str, Any]] = [
        {"value": service.json_value(v), "label": service.json_value(v)} for v in values
    ]
    return {"values": items[:LIMIT], "more": len(items) > LIMIT}


def _fetch(qs) -> list:
    """Evaluate on the reports database under the statement timeout."""
    alias = reports_database()
    with reporting(alias), statement_timeout(alias):
        return list(qs.using(alias))
