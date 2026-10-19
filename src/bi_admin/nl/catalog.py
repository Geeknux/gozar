"""What the NL layer knows about: the user's visible catalog, phrase matching and
retrieval of the entries relevant to a question.

The catalog here is *metadata only* — keys, labels, synonyms, descriptions,
types and declared choices — restricted to the semantic models the user may
report on and the dimensions/measures they may see (M4 visibility). Row data
never enters it; category values live in :mod:`bi_admin.nl.values` and stay on
the server.

Retrieval is lexical (token phrases with Persian suffix tolerance, character
trigrams and transliteration keys) so it needs no embedding service; a project
can swap it via ``GOZAR["NL"]["RETRIEVER"]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bi_admin.nl.text import normalise, phonetic_similarity, similarity, tokens
from bi_admin.semantic import semantic_site

# Persian plural/ezafe/adjective suffixes and English plurals a catalog word may carry
# in a question: "شهرها" matches "شهر", "cities" matches "city".
SUFFIXES = ("هایی", "های", "ها", "یی", "ای", "ی", "ان", "es", "s")


@dataclass
class Item:
    kind: str  # "measure" | "dimension"
    key: str
    label: dict[str, str]
    synonyms: dict[str, list[str]]
    description: dict[str, str]
    type: str | None = None  # dimension type
    format: str | None = None  # measure format
    choices: list[tuple[Any, dict[str, str]]] | None = None

    def phrases(self) -> list[str]:
        out = [self.key.replace("_", " "), *self.label.values()]
        out += [s for v in self.synonyms.values() for s in v]
        return list(dict.fromkeys(p for p in out if p))

    def prompt(self, lang: str | None = None) -> dict[str, Any]:
        """The entry as sent to an LLM (both languages unless ``lang``)."""
        pick = (lambda v: v) if lang is None else (lambda v: v.get(lang))
        out: dict[str, Any] = {"key": self.key, "label": pick(self.label)}
        syn = {k: v for k, v in self.synonyms.items() if v}
        if syn:
            out["synonyms"] = syn if lang is None else syn.get(lang, [])
        desc = {k: v for k, v in self.description.items() if v}
        if desc:
            out["description"] = pick(desc) if lang is None else desc.get(lang, "")
        if self.type:
            out["type"] = self.type
        if self.format:
            out["format"] = self.format
        if self.choices:
            out["choices"] = [{"value": v, "label": pick(labels)} for v, labels in self.choices]
        return out


@dataclass
class ModelEntry:
    name: str
    label: dict[str, str]
    synonyms: dict[str, list[str]]
    description: dict[str, str]
    time_dimension: str | None
    measures: dict[str, Item]
    dimensions: dict[str, Item]
    examples: list[dict[str, Any]] = field(default_factory=list)

    def phrases(self) -> list[str]:
        out = [self.name.replace("_", " "), *self.label.values()]
        out += [s for v in self.synonyms.values() for s in v]
        return list(dict.fromkeys(p for p in out if p))

    def items(self) -> list[Item]:
        return [*self.measures.values(), *self.dimensions.values()]

    def subset(self, keys: set[str]) -> ModelEntry:
        return ModelEntry(
            self.name,
            self.label,
            self.synonyms,
            self.description,
            self.time_dimension,
            {k: v for k, v in self.measures.items() if k in keys},
            {k: v for k, v in self.dimensions.items() if k in keys},
            self.examples,
        )

    def prompt(self, lang: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "semantic_model": self.name,
            "label": self.label if lang is None else self.label.get(lang),
        }
        desc = {k: v for k, v in self.description.items() if v}
        if desc:
            out["description"] = desc if lang is None else desc.get(lang, "")
        out["default_time_dimension"] = self.time_dimension
        out["measures"] = [m.prompt(lang) for m in self.measures.values()]
        out["dimensions"] = [d.prompt(lang) for d in self.dimensions.values()]
        return out


def visible_catalog(request) -> list[ModelEntry]:
    """Every semantic model ``request.user`` may report on, with only the items they
    may see (``request=None``: trusted code, everything)."""
    out = []
    for sem in semantic_site.all():
        if request is not None and not sem.has_permission(request):
            continue
        dims = sem.visible_dimensions(request)
        measures = {
            k: Item("measure", k, m.label, m.synonyms, m.description, format=m.format)
            for k, m in sem.visible_measures(request).items()
        }
        dimensions = {
            k: Item(
                "dimension",
                k,
                d.label,
                d.synonyms,
                d.description,
                type=d.type,
                choices=list(d.choices) if d.choices else None,
            )
            for k, d in dims.items()
        }
        examples = [
            e for e in getattr(sem, "nl_examples", None) or [] if _uses_only(e, {*dims, *measures})
        ]
        out.append(
            ModelEntry(
                sem.name,
                sem.label,
                sem.synonyms,
                sem.description,
                sem.default_time_dimension(request),
                measures,
                dimensions,
                examples,
            )
        )
    return out


def _uses_only(example: dict, visible: set[str]) -> bool:
    """Few-shot examples may only mention items the user can see."""
    spec = example.get("spec") or {}
    keys = set(spec.get("measures") or []) | set(spec.get("dimensions") or [])
    keys |= {f.get("field") for f in spec.get("filters") or []}
    keys |= {s.get("field") for s in spec.get("sort") or []}
    if spec.get("time_dimension"):
        keys.add(spec["time_dimension"])
    return keys <= visible


# --- phrase matching ----------------------------------------------------------------


# Persian plural markers written as separate words ("سفارش ها" after ZWNJ → space)
PLURAL_WORDS = {"ها", "های", "هایی"}
# words the catalog may spell differently: abbreviations and plain synonyms
ABBREVIATIONS = {
    "avg": "average",
    "mean": "average",
    "qty": "quantity",
    "no": "number",
    "amt": "amount",
    "متوسط": "میانگین",
}


def token_matches(word: str, target: str) -> bool:
    """``word`` (from a question) is ``target`` (from the catalog), allowing a plural
    or ezafe suffix on the question side."""
    if word == target or ABBREVIATIONS.get(word) == target or ABBREVIATIONS.get(target) == word:
        return True
    if word in PLURAL_WORDS and target in PLURAL_WORDS:
        return True
    if len(target) >= 3 and word.startswith(target):
        return word[len(target) :] in SUFFIXES
    if target.endswith("y") and word == target[:-1] + "ies":  # city → cities
        return True
    return False


@dataclass(frozen=True)
class Hit:
    start: int
    end: int  # exclusive
    payload: Any
    phrase: str

    @property
    def size(self) -> int:
        return self.end - self.start


def find_phrases(words: list[str], phrases: list[tuple[str, Any]], taken=None) -> list[Hit]:
    """Non-overlapping occurrences of ``phrases`` in ``words``, longest first.

    ``taken`` (a set of word indexes) marks words already claimed; matching words
    are added to it."""
    taken = set() if taken is None else taken
    candidates: list[Hit] = []
    for phrase, payload in phrases:
        ptoks = tokens(phrase)
        # "سفارش ها" in the catalog also matches a bare "سفارش" in the question
        variants = [ptoks, ptoks[:-1]] if len(ptoks) > 1 and ptoks[-1] in PLURAL_WORDS else [ptoks]
        for toks in variants:
            n = len(toks)
            for i in range(len(words) - n + 1):
                if all(token_matches(words[i + j], toks[j]) for j in range(n)):
                    candidates.append(Hit(i, i + n, payload, phrase))
    # longest phrase first; then earlier position
    candidates.sort(key=lambda h: (-h.size, -len(h.phrase), h.start))
    hits = []
    for hit in candidates:
        span = set(range(hit.start, hit.end))
        if span & taken:
            continue
        taken |= span
        hits.append(hit)
    return sorted(hits, key=lambda h: h.start)


def _trigrams(s: str) -> set[str]:
    s = f"  {s} "
    return {s[i : i + 3] for i in range(len(s) - 2)}


def phrase_score(question: str, words: list[str], phrase: str) -> float:
    """How strongly ``question`` mentions ``phrase``: 1 for a whole-phrase match,
    otherwise a weaker character/transliteration similarity."""
    if find_phrases(words, [(phrase, None)]):
        return 1.0
    p = normalise(phrase)
    if not p:
        return 0.0
    tri_q, tri_p = _trigrams(question), _trigrams(p)
    overlap = len(tri_q & tri_p) / len(tri_p) if tri_p else 0.0
    best_word = max((similarity(w, p) for w in words), default=0.0)
    score = max(overlap * 0.6, best_word * 0.7 if best_word >= 0.8 else 0.0)
    if len(p) >= 4 and any(phonetic_similarity(w, p) == 1.0 for w in words if len(w) >= 4):
        score = max(score, 0.6)  # "revenue" written in Persian letters, etc.
    return score


def item_score(question: str, words: list[str], item: Item | ModelEntry) -> float:
    return max((phrase_score(question, words, p) for p in item.phrases()), default=0.0)


def retrieve(
    question: str,
    entries: list[ModelEntry],
    *,
    previous: dict | None = None,
    top_k: int | None = 20,
    max_models: int = 3,
) -> list[ModelEntry]:
    """The catalog subset to put in front of the model for ``question``.

    Models are ranked by their own label plus their best items; each keeps its
    ``top_k`` best items, always including the default time dimension and every
    item of the ``previous`` spec (refinement). Small catalogs come back whole."""
    if not entries:
        return []
    q = normalise(question)
    words = tokens(question)
    ranked = []
    for entry in entries:
        item_scores = {i.key: item_score(q, words, i) for i in entry.items()}
        best = sorted(item_scores.values(), reverse=True)[:3]
        score = item_score(q, words, entry) + sum(best)
        if previous and previous.get("semantic_model") == entry.name:
            score += 10  # refining: keep the previous model in front
        ranked.append((score, entry, item_scores))
    ranked.sort(key=lambda r: -r[0])
    out = []
    for _score, entry, item_scores in ranked[:max_models]:
        if top_k is None or len(item_scores) <= top_k:
            out.append(entry)
            continue
        keep = {k for k, _ in sorted(item_scores.items(), key=lambda kv: -kv[1])[:top_k]}
        if entry.time_dimension:
            keep.add(entry.time_dimension)
        if previous and previous.get("semantic_model") == entry.name:
            keep |= set(previous.get("measures") or []) | set(previous.get("dimensions") or [])
            keep |= {f.get("field") for f in previous.get("filters") or []}
            if previous.get("time_dimension"):
                keep.add(previous["time_dimension"])
        if not any(k in entry.measures for k in keep):  # always offer at least one measure
            keep.add(next(iter(entry.measures)))
        out.append(entry.subset(keep))
    return out
