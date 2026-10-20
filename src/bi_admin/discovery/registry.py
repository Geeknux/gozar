"""Model discovery: an introspected, cached registry of models, fields and relations.

Everything here is read from ``Model._meta``; nothing is declared by hand. The
registry feeds the auto-generated CRUD (M1) and, later, the semantic layer.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from django.apps import apps
from django.conf import settings
from django.db import models
from django.utils import translation
from django.utils.encoding import force_str

FieldKind = Literal[
    "string",
    "text",
    "integer",
    "decimal",
    "float",
    "boolean",
    "date",
    "datetime",
    "time",
    "duration",
    "uuid",
    "json",
    "email",
    "url",
    "file",
    "fk",
    "o2o",
    "m2m",
    "other",
]

RelationKind = Literal["fk", "o2o", "m2m", "reverse_fk", "reverse_o2o", "reverse_m2m"]

# Apps hidden by default: framework plumbing nobody edits by hand.
DEFAULT_EXCLUDE_APPS = ("contenttypes", "sessions", "admin", "bi_admin", "messages", "staticfiles")
DEFAULT_EXCLUDE_MODELS = ("auth.permission",)

_INTERNAL_KIND: dict[str, FieldKind] = {
    "AutoField": "integer",
    "BigAutoField": "integer",
    "SmallAutoField": "integer",
    "IntegerField": "integer",
    "BigIntegerField": "integer",
    "SmallIntegerField": "integer",
    "PositiveIntegerField": "integer",
    "PositiveBigIntegerField": "integer",
    "PositiveSmallIntegerField": "integer",
    "DecimalField": "decimal",
    "FloatField": "float",
    "BooleanField": "boolean",
    "NullBooleanField": "boolean",
    "CharField": "string",
    "SlugField": "string",
    "GenericIPAddressField": "string",
    "TextField": "text",
    "EmailField": "email",
    "URLField": "url",
    "DateField": "date",
    "DateTimeField": "datetime",
    "TimeField": "time",
    "DurationField": "duration",
    "UUIDField": "uuid",
    "JSONField": "json",
    "FileField": "file",
    "ImageField": "file",
    "FilePathField": "string",
    "ForeignKey": "fk",
    "OneToOneField": "o2o",
    "ManyToManyField": "m2m",
}


def model_label(model: type[models.Model]) -> str:
    """``app_label.model_name`` (lowercase), the canonical id used everywhere."""
    return model._meta.label_lower


@dataclass(frozen=True)
class FieldInfo:
    name: str
    verbose_name: str
    kind: FieldKind
    internal_type: str
    primary_key: bool = False
    editable: bool = True
    null: bool = False
    blank: bool = False
    unique: bool = False
    required: bool = False
    has_default: bool = False
    max_length: int | None = None
    max_digits: int | None = None
    decimal_places: int | None = None
    help_text: str = ""
    choices: list[tuple[Any, str]] | None = None
    related_model: str | None = None  # model label for fk/o2o/m2m
    db_index: bool = False
    image: bool = False  # an ImageField (its internal type is "FileField")

    @property
    def is_relation(self) -> bool:
        return self.kind in ("fk", "o2o", "m2m")

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.choices is not None:
            data["choices"] = [{"value": v, "label": label} for v, label in self.choices]
        return data


@dataclass(frozen=True)
class RelationInfo:
    """A directed edge of the relation graph (one per direction)."""

    name: str  # lookup name usable in ORM paths (``customer``, ``orders``)
    kind: RelationKind
    source: str  # model label
    target: str  # model label
    verbose_name: str
    many: bool  # True when following it can multiply rows

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ModelInfo:
    label: str
    app_label: str
    model_name: str
    object_name: str
    verbose_name: str
    verbose_name_plural: str
    app_verbose_name: str
    pk_name: str
    ordering: list[str]
    fields: dict[str, FieldInfo] = field(default_factory=dict)
    relations: dict[str, RelationInfo] = field(default_factory=dict)

    @property
    def model(self) -> type[models.Model]:
        return apps.get_model(self.label)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "app_label": self.app_label,
            "model_name": self.model_name,
            "object_name": self.object_name,
            "verbose_name": self.verbose_name,
            "verbose_name_plural": self.verbose_name_plural,
            "app_verbose_name": self.app_verbose_name,
            "pk_name": self.pk_name,
            "ordering": self.ordering,
            "fields": [f.as_dict() for f in self.fields.values()],
            "relations": [r.as_dict() for r in self.relations.values()],
        }


def _field_info(f: models.Field) -> FieldInfo:
    internal = f.get_internal_type()
    kind = _INTERNAL_KIND.get(internal, "other")
    choices = None
    if f.choices:
        choices = [(value, force_str(label)) for value, label in f.flatchoices]
    has_default = f.has_default() or (
        getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False)
    )
    editable = bool(f.editable) and not f.auto_created
    required = editable and not f.blank and not has_default and kind != "m2m"
    return FieldInfo(
        name=f.name,
        verbose_name=force_str(f.verbose_name),
        kind=kind,
        internal_type=internal,
        primary_key=bool(f.primary_key),
        editable=editable,
        null=bool(f.null),
        blank=bool(f.blank),
        unique=bool(f.unique),
        required=required,
        has_default=has_default,
        max_length=getattr(f, "max_length", None),
        max_digits=getattr(f, "max_digits", None),
        decimal_places=getattr(f, "decimal_places", None),
        help_text=force_str(f.help_text or ""),
        choices=choices,
        related_model=model_label(f.related_model) if f.is_relation and f.related_model else None,
        db_index=bool(getattr(f, "db_index", False)),
        image=isinstance(f, models.ImageField),
    )


def _relations(model: type[models.Model]) -> dict[str, RelationInfo]:
    src = model_label(model)
    out: dict[str, RelationInfo] = {}
    for f in model._meta.get_fields(include_hidden=False):
        if not f.is_relation or f.related_model is None or isinstance(f.related_model, str):
            continue
        if getattr(f, "is_generic", False) or f.__class__.__name__.startswith("Generic"):
            continue
        tgt = model_label(f.related_model)
        if f.auto_created and not f.concrete:  # reverse side
            if f.one_to_one:
                kind: RelationKind = "reverse_o2o"
            elif f.many_to_many:
                kind = "reverse_m2m"
            else:
                kind = "reverse_fk"
            name = f.name  # ForeignObjectRel.name is the related query name
            verbose = force_str(f.related_model._meta.verbose_name_plural)
        else:
            kind = "o2o" if f.one_to_one else "m2m" if f.many_to_many else "fk"
            name = f.name
            verbose = force_str(getattr(f, "verbose_name", f.name))
        out[name] = RelationInfo(
            name=name,
            kind=kind,
            source=src,
            target=tgt,
            verbose_name=verbose,
            many=kind in ("m2m", "reverse_fk", "reverse_m2m"),
        )
    return out


def build_model_info(model: type[models.Model]) -> ModelInfo:
    opts = model._meta
    fields: dict[str, FieldInfo] = {}
    for f in opts.get_fields(include_hidden=False):
        # Concrete fields + forward m2m; reverse relations live in ``relations``.
        if f.auto_created and not f.concrete:
            continue
        if not getattr(f, "concrete", False) and not f.many_to_many:
            continue
        fields[f.name] = _field_info(f)
    return ModelInfo(
        label=model_label(model),
        app_label=opts.app_label,
        model_name=opts.model_name,
        object_name=opts.object_name,
        verbose_name=force_str(opts.verbose_name),
        verbose_name_plural=force_str(opts.verbose_name_plural),
        app_verbose_name=force_str(opts.app_config.verbose_name),
        pk_name=opts.pk.name,
        ordering=list(opts.ordering or []),
        fields=fields,
        relations=_relations(model),
    )


@dataclass(frozen=True)
class PathStep:
    relation: str
    source: str
    target: str
    many: bool


class Registry:
    """Introspected models plus the relation graph between them."""

    def __init__(self, models_: dict[str, ModelInfo]):
        self.models = models_

    def __contains__(self, label: str) -> bool:
        return label.lower() in self.models

    def __iter__(self):
        return iter(self.models.values())

    def __len__(self) -> int:
        return len(self.models)

    def get(self, label: str) -> ModelInfo:
        try:
            return self.models[label.lower()]
        except KeyError:
            raise LookupError(f"Model {label!r} is not in the Gozar registry") from None

    def edges(self) -> list[RelationInfo]:
        """Relation-graph edges whose both ends are registered models."""
        return [
            r for m in self.models.values() for r in m.relations.values() if r.target in self.models
        ]

    def neighbours(self, label: str) -> list[RelationInfo]:
        return [r for r in self.get(label).relations.values() if r.target in self.models]

    def find_path(self, source: str, target: str, max_depth: int = 4) -> list[PathStep] | None:
        """Shortest relation path ``source → … → target`` (BFS), preferring to-one hops."""
        source, target = source.lower(), target.lower()
        self.get(source), self.get(target)
        if source == target:
            return []
        queue: deque[tuple[str, list[PathStep]]] = deque([(source, [])])
        seen = {source}
        while queue:
            label, path = queue.popleft()
            if len(path) >= max_depth:
                continue
            # to-one edges first so equal-length paths prefer non-multiplying joins
            for rel in sorted(self.neighbours(label), key=lambda r: r.many):
                if rel.target in seen:
                    continue
                step = PathStep(rel.name, rel.source, rel.target, rel.many)
                if rel.target == target:
                    return [*path, step]
                seen.add(rel.target)
                queue.append((rel.target, [*path, step]))
        return None

    def resolve_lookup(self, source: str, lookup: str) -> tuple[list[PathStep], FieldInfo]:
        """Resolve an ORM lookup path such as ``customer__city__name``.

        Returns the relation steps walked and the final field. Raises
        ``LookupError`` for unknown parts — callers use this to validate
        user/LLM-supplied paths before they ever reach the ORM.
        """
        parts = lookup.split("__")
        current = self.get(source)
        steps: list[PathStep] = []
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            rel = current.relations.get(part)
            if rel is not None and (not last or part not in current.fields):
                if rel.target not in self.models:
                    raise LookupError(f"{current.label}.{part} points outside the registry")
                steps.append(PathStep(rel.name, rel.source, rel.target, rel.many))
                current = self.get(rel.target)
                if last:
                    return steps, current.fields[current.pk_name]
                continue
            if last and part in current.fields:
                return steps, current.fields[part]
            raise LookupError(f"Unknown field {part!r} on {current.label} (in {lookup!r})")
        raise LookupError(f"Empty lookup {lookup!r}")

    def lookup_paths(self, source: str, max_depth: int = 2, to_one_only: bool = True) -> list[str]:
        """All field lookup paths reachable from ``source`` (e.g. for filter pickers)."""
        out: list[str] = []

        def walk(label: str, prefix: str, depth: int, visited: frozenset[str]) -> None:
            info = self.get(label)
            for name, f in info.fields.items():
                if f.kind != "m2m":
                    out.append(prefix + name)
            if depth >= max_depth:
                return
            for rel in self.neighbours(label):
                if (to_one_only and rel.many) or rel.target in visited:
                    continue
                walk(rel.target, f"{prefix}{rel.name}__", depth + 1, visited | {rel.target})

        walk(source.lower(), "", 0, frozenset({source.lower()}))
        return out

    def graph(self) -> dict[str, Any]:
        return {
            "nodes": [
                {"label": m.label, "verbose_name": m.verbose_name, "app_label": m.app_label}
                for m in self.models.values()
            ],
            "edges": [e.as_dict() for e in self.edges()],
        }


def _config() -> dict[str, Any]:
    return getattr(settings, "GOZAR", {}) or {}


def is_included(model: type[models.Model]) -> bool:
    cfg = _config()
    opts = model._meta
    if opts.abstract or (opts.proxy and cfg.get("EXCLUDE_PROXY", False)):
        return False
    include_apps = cfg.get("INCLUDE_APPS")
    if include_apps is not None and opts.app_label not in include_apps:
        return False
    exclude_apps = cfg.get("EXCLUDE_APPS", DEFAULT_EXCLUDE_APPS)
    exclude_models = {m.lower() for m in cfg.get("EXCLUDE_MODELS", DEFAULT_EXCLUDE_MODELS)}
    return opts.app_label not in exclude_apps and opts.label_lower not in exclude_models


def discover() -> Registry:
    infos = {model_label(m): build_model_info(m) for m in apps.get_models() if is_included(m)}
    return Registry(infos)


_lock = threading.Lock()
_registries: dict[str, Registry] = {}


def get_registry() -> Registry:
    """The process-wide registry for the active language, built lazily on first use.

    Verbose names and choice labels are translated, so one registry is kept per
    language (``translation.get_language()``)."""
    lang = translation.get_language() or settings.LANGUAGE_CODE
    registry = _registries.get(lang)
    if registry is None:
        with _lock:
            registry = _registries.get(lang)
            if registry is None:
                registry = _registries[lang] = discover()
    return registry


def reset_registry() -> None:
    """Drop the cached registries (tests, settings changes, app reloads)."""
    with _lock:
        _registries.clear()
