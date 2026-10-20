"""Scale (M7): pre-aggregations and columnar report engines.

* ``PreAggregation`` — summary tables declared on a semantic model, answered from
  when provably exact (``matcher``), refreshed by ``refresh``;
* ``columnar`` — optional analytical copies (DuckDB) selected per semantic model;
* ``planner`` — picks the engine for each report and reports the choice.
"""

from bi_admin.scale.preaggregations import PreAggregation

__all__ = ["PreAggregation"]
