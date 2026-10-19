"""Category value matching: the user writes ``تهران``, the database says ``Tehran``.

A *value index* lists the distinct values of one category dimension — read from
the user's **scoped** semantic queryset (row rules, custom ``get_queryset``), so
a user can neither match nor be offered a value outside their scope. Choice
dimensions index their declared choices (both languages); dimensions ending on a
relation index the related objects of the target's scoped queryset by their
``str()``.

Matching tries, in order: the exact normalised text (Arabic/Persian letters,
digits, case, ZWNJ), the transliteration key across scripts (``تهران`` ⇄
``Tehran``), then fuzzy similarity. Indexes are cached per (dimension, scope,
data version), like report results, and never leave the server: the LLM only
ever sees the user's own words.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from django.db.models import F

from bi_admin.access.rules import scope_key, semantic_queryset, target_queryset
from bi_admin.execution import cache
from bi_admin.execution.router import reports_database
from bi_admin.nl.conf import nl_setting
from bi_admin.nl.text import is_persian, normalise, phonetic_keys, similarity
from bi_admin.semantic import Dimension, Semantic

MATCH_THRESHOLD = 0.8  # fuzzy matches below this are only suggestions
MENTION_THRESHOLD = 0.88  # fuzzy matches of words found in a question


@dataclass(frozen=True)
class Match:
    value: Any
    label: str
    score: float
    how: str  # "exact" | "phonetic" | "fuzzy"


@dataclass
class ValueIndex:
    """The values of one dimension that one user may see."""

    dimension: str
    entries: list[tuple[Any, str]]  # (value, label)
    truncated: bool = False
    _norm: dict[str, list[int]] = field(default_factory=dict, repr=False)
    _keys: dict[str, list[int]] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        for i, (value, label) in enumerate(self.entries):
            for text in {label, str(value)}:
                self._norm.setdefault(normalise(text), []).append(i)
                for key in phonetic_keys(text):
                    self._keys.setdefault(key, []).append(i)

    def __len__(self) -> int:
        return len(self.entries)

    def _match(self, i: int, score: float, how: str) -> Match:
        value, label = self.entries[i]
        return Match(value, label, score, how)

    def exact(self, text: str) -> list[Match]:
        """Exact or cross-script transliteration matches of ``text``."""
        norm = normalise(text)
        found = [self._match(i, 1.0, "exact") for i in dict.fromkeys(self._norm.get(norm, []))]
        if found:
            return found
        out: dict[int, Match] = {}
        for key in phonetic_keys(text):
            if len(key.replace(" ", "")) < 2:
                continue
            for i in self._keys.get(key, []):
                value, label = self.entries[i]
                # only across scripts (or for keys long enough to be distinctive)
                if is_persian(norm) != is_persian(label) or len(key) >= 4:
                    out.setdefault(i, self._match(i, 0.95, "phonetic"))
        return list(out.values())

    def search(self, text: str, limit: int = 5, cutoff: float = 0.5) -> list[Match]:
        """Best matches of ``text``, exact first, then fuzzy (letters or sounds)."""
        found = self.exact(text)
        if found:
            return found[:limit]
        norm = normalise(text)
        scored: dict[int, float] = {}
        names = list(self._norm)
        for name in difflib.get_close_matches(norm, names, n=limit * 2, cutoff=cutoff):
            for i in self._norm[name]:
                scored[i] = max(scored.get(i, 0.0), similarity(norm, name))
        keys = phonetic_keys(text)
        for key in keys:
            for close in difflib.get_close_matches(key, list(self._keys), n=limit * 2, cutoff=0.75):
                for i in self._keys[close]:
                    scored[i] = max(scored.get(i, 0.0), similarity(key, close) * 0.95)
        ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
        out, seen = [], set()
        for i, s in ranked:
            value = repr(self.entries[i][0])
            if s >= cutoff and value not in seen:  # one entry per value (fa/en labels)
                seen.add(value)
                out.append(self._match(i, round(s, 3), "fuzzy"))
        return out[:limit]

    def resolve(self, text: str) -> tuple[Match | None, list[Match]]:
        """The value ``text`` means (or ``None``) plus the alternatives considered.

        A fuzzy match must clear ``MATCH_THRESHOLD`` and beat the runner-up."""
        found = self.search(text)
        if not found:
            return None, []
        best = found[0]
        if best.how != "fuzzy":
            exact = {repr(m.value) for m in found if m.how == best.how}
            return (best if len(exact) == 1 else None), found
        if best.score >= MATCH_THRESHOLD and (
            len(found) == 1 or best.score - found[1].score >= 0.05
        ):
            return best, found
        return None, found


def _choice_index(dim: Dimension) -> ValueIndex:
    entries = []
    for value, labels in dim.choices or []:
        entries.append((value, labels.get("en") or str(value)))
        if labels.get("fa"):
            entries.append((value, labels["fa"]))
    return ValueIndex(dim.key, entries)


def ends_on_relation(dim: Dimension) -> bool:
    return dim.path is not None and (
        (dim.field is not None and dim.field.is_relation)
        or len(dim.steps) == len(dim.path.split("__"))
    )


def _source(sem: Semantic, dim: Dimension, request):
    """(queryset of (value, label) rows, cache dependencies)."""
    from bi_admin.discovery import model_label

    if ends_on_relation(dim):
        target = dim.field.related_model if dim.field.is_relation else dim.steps[-1].target
        qs = target_queryset(target, request)
        return qs, [target if isinstance(target, str) else model_label(target)]
    if dim.path is not None and dim.steps and dim.field is not None:
        # A field on a related model (customer__city__name): every value of the
        # related table in the user's scope, not only those that have rows yet —
        # "orders in Rasht" is 0 orders, not an unknown city.
        target = dim.steps[-1].target
        qs = target_queryset(target, request).annotate(_gv=F(dim.field.name))
        return qs.filter(_gv__isnull=False), [target]
    expr = dim.expr if dim.path is None else F(dim.path)
    qs = semantic_queryset(sem, request).annotate(_gv=expr).filter(_gv__isnull=False)
    return qs, [sem.model_label]


def build_index(sem: Semantic, dim: Dimension, request) -> ValueIndex:
    """The (cached) value index of ``dim`` for the user behind ``request``."""
    if dim.choices:
        return _choice_index(dim)
    from bi_admin.crud.service import json_value
    from bi_admin.engine.values import _fetch

    qs, always = _source(sem, dim, request)
    limit = int(nl_setting("VALUE_INDEX_MAX"))
    alias = reports_database()
    timeout = int(nl_setting("VALUE_INDEX_TIMEOUT") or 0)
    key = None
    if timeout > 0:
        try:
            sql = str(qs.query)
        except Exception:
            sql = ""
        deps = cache.dependencies(sql, alias, always)
        raw = json.dumps(
            {
                "semantic": sem.name,
                "dimension": dim.key,
                "scope": scope_key(qs),
                "database": alias,
                "data": cache.versions(deps),
                "limit": limit,
            },
            sort_keys=True,
            default=str,
        )
        key = "gozar:nlvalues:" + hashlib.sha256(raw.encode()).hexdigest()
        hit = cache.backend().get(key)
        if hit is not None:
            return ValueIndex(dim.key, [tuple(e) for e in hit["entries"]], hit["truncated"])
    if ends_on_relation(dim):
        objs = _fetch(qs.order_by("pk")[: limit + 1])
        entries = [(json_value(o.pk), str(o)) for o in objs]
    else:
        values = _fetch(qs.order_by("_gv").values_list("_gv", flat=True).distinct()[: limit + 1])
        entries = [(json_value(v), str(json_value(v))) for v in values]
    truncated = len(entries) > limit
    entries = entries[:limit]
    if key is not None:
        cache.backend().set(key, {"entries": entries, "truncated": truncated}, timeout)
    return ValueIndex(dim.key, entries, truncated)


class ValueMatcher:
    """Value indexes of one semantic model for one request, built on first use."""

    def __init__(self, sem: Semantic, request):
        self.sem = sem
        self.request = request
        self.dimensions = {
            k: d
            for k, d in sem.visible_dimensions(request).items()
            if d.type in ("category", "geo")
        }
        self._indexes: dict[str, ValueIndex] = {}

    def index(self, key: str) -> ValueIndex | None:
        dim = self.dimensions.get(key)
        if dim is None:
            return None
        if key not in self._indexes:
            self._indexes[key] = build_index(self.sem, dim, self.request)
        return self._indexes[key]

    def indexes(self) -> dict[str, ValueIndex]:
        return {k: idx for k in self.dimensions if (idx := self.index(k)) is not None}

    def mentions(self, text: str, fuzzy: bool = True) -> dict[str, list[Match]]:
        """Values of every category dimension that ``text`` names exactly (or,
        with ``fuzzy``, very closely)."""
        out: dict[str, list[Match]] = {}
        for key, idx in self.indexes().items():
            found = idx.exact(text)
            if not found and fuzzy and len(normalise(text)) >= 5:
                found = [m for m in idx.search(text, limit=2, cutoff=MENTION_THRESHOLD)]
            if found:
                out[key] = found
        return out
