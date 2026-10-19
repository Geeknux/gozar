"""Natural-language interface (DESIGN.md §5): a question in English or Persian →
a validated ``ReportSpec``.

* ``text`` — normalisation (letters, digits, ZWNJ) and Persian ⇄ Latin keys;
* ``catalog`` — the user's visible catalog, phrase matching, retrieval;
* ``values`` — the scoped category value index and matching;
* ``prompt`` / ``providers`` — the LLM prompt and providers (OpenAI-compatible,
  offline ``heuristic``, ``stub``);
* ``pipeline`` — ``ask()``: retrieval → provider → validation/retries → value
  matching → interpretation, with cache and audit (``NLLog``);
* ``interpret`` — the interpretation sentence; ``evaluation`` — golden-set scoring.
"""
