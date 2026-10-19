"""Access control: row-level rules and the report audit log (see DESIGN.md §3.5).

Model permissions come from Django; semantic visibility lives on ``Dimension``
and ``Measure`` (``permission=``)."""

from bi_admin.access.rules import (
    model_filter,
    model_queryset,
    rule,
    semantic_filter,
    semantic_queryset,
)

__all__ = ["model_filter", "model_queryset", "rule", "semantic_filter", "semantic_queryset"]
