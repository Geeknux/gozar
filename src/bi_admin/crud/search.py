"""Global search: one query string across every model the user may view.

Each model is searched with its own ``search_fields`` (the list's search rules,
Arabic/Persian letter variants included) over its row-scoped queryset, so a hit
is exactly a row the user would find on that model's list."""

from __future__ import annotations

from typing import Any

from django.utils.encoding import force_str
from django.utils.text import capfirst

from bi_admin.access.rules import model_queryset
from bi_admin.conf import setting
from bi_admin.crud.service import apply_ordering, apply_search, json_value, visible_models

MIN_LENGTH = 2
COUNT_CAP = 1000  # "1000+" beyond this: counting a huge table isn't worth it


def global_search(request, query: str, per_model: int | None = None) -> dict[str, Any]:
    query = (query or "").strip()
    if len(query) < MIN_LENGTH:
        return {"q": query, "groups": [], "total": 0}
    per_model = per_model or setting("GLOBAL_SEARCH_PER_MODEL")
    groups = []
    for opts in visible_models(request):
        if not opts.get_search_fields():
            continue
        qs = apply_search(opts, model_queryset(opts, request), query)
        count = qs.order_by()[: COUNT_CAP + 1].count()
        if not count:
            continue
        hits = apply_ordering(opts, qs, None)[:per_model]
        info = opts.info
        groups.append(
            {
                "model": opts.label,
                "verbose_name": capfirst(info.verbose_name),
                "verbose_name_plural": capfirst(info.verbose_name_plural),
                "app_label": info.app_label,
                "app_verbose_name": capfirst(info.app_verbose_name),
                "count": min(count, COUNT_CAP),
                "more": count > COUNT_CAP,
                "results": [{"pk": json_value(o.pk), "label": force_str(o)} for o in hits],
            }
        )
    groups.sort(key=lambda g: -g["count"])
    return {"q": query, "groups": groups, "total": sum(g["count"] for g in groups)}
