"""Declarative semantic layer: business-level dimensions and measures over models.

A project describes what can be reported on, once, in an app's ``gozar.py``::

    from django.db.models import Count, Q, Sum
    from bi_admin.semantic import Dimension, Measure, Semantic, register

    @register
    class OrderMetrics(Semantic):
        model = Order
        time_dimension = "created_at"
        dimensions = {
            "created_at": Dimension("created_at", type="time"),
            "city": Dimension("customer__city__name", label={"fa": "شهر", "en": "City"}),
        }
        measures = {
            "revenue": Measure(Sum("amount", filter=Q(status="paid")), format="currency"),
            "orders": Measure(Count("id")),
        }

Labels, synonyms and descriptions (Persian + English) are the context the NL
interface works from; the report engine validates every spec against them.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from typing import Any, Literal

from django.core.exceptions import FieldError, ImproperlyConfigured
from django.db import models
from django.db.models import F
from django.db.models.expressions import BaseExpression
from django.utils import translation
from django.utils.encoding import force_str
from django.utils.functional import Promise
from django.utils.text import capfirst
from django.utils.translation import gettext

from bi_admin.discovery import FieldInfo, PathStep, get_registry, model_label

LANGS = ("fa", "en")
DimensionType = Literal["time", "category", "number", "boolean", "geo"]
DIMENSION_TYPES: tuple[str, ...] = ("time", "category", "number", "boolean", "geo")
MeasureFormat = Literal["number", "integer", "currency", "percent", "decimal"]
MEASURE_FORMATS: tuple[str, ...] = ("number", "integer", "currency", "percent", "decimal")
TIME_GRAINS = ("day", "week", "month", "season", "year")
CALENDARS = ("jalali", "gregorian")

_KIND_TO_TYPE = {
    "date": "time",
    "datetime": "time",
    "integer": "number",
    "decimal": "number",
    "float": "number",
    "boolean": "boolean",
}

Text = dict[str, str]
# Who may see a dimension/measure: a permission name ("shop.view_margin"), several
# (all required), or a predicate on the user. ``None``: whoever sees the model.
Permission = str | list[str] | tuple[str, ...] | Callable[[Any], bool] | None
TextInput = str | Promise | dict[str, str] | None
SynonymsInput = list[str] | tuple[str, ...] | dict[str, list[str]] | None


def text(value: TextInput, fa: str = "", en: str = "") -> Text:
    """Normalise a label/description to ``{"fa": …, "en": …}``.

    A bare string is English source text and goes through gettext, so
    ``label=gettext_lazy("Revenue")`` picks up the Persian catalog. A missing
    language falls back to the given default, then to the other language.
    """
    if value is None:
        value = {}
    elif isinstance(value, str | Promise):
        value = in_languages(lambda: gettext(force_str(value)))
    else:
        value = {k: force_str(v) for k, v in value.items()}
    out = {"fa": value.get("fa") or fa, "en": value.get("en") or en}
    out["fa"] = out["fa"] or out["en"]
    out["en"] = out["en"] or out["fa"]
    return out


def synonyms(value: SynonymsInput) -> dict[str, list[str]]:
    if value is None:
        return {lang: [] for lang in LANGS}
    if isinstance(value, list | tuple):  # one list of source-language synonyms
        return {lang: [force_str(v) for v in value] for lang in LANGS}
    return {lang: list(value.get(lang, [])) for lang in LANGS}


def in_languages(fn) -> Text:
    """``fn()`` evaluated (and stringified) with each supported language active."""
    out = {}
    for lang in LANGS:
        with translation.override(lang):
            out[lang] = force_str(fn())
    return out


def active_language() -> str:
    lang = (translation.get_language() or "en").split("-")[0]
    return lang if lang in LANGS else "en"


def humanize(key: str) -> str:
    return key.replace("__", " ").replace("_", " ").strip().capitalize()


def localize(value: dict, lang: str | None):
    """Pick one language from a ``{"fa":…, "en":…}`` mapping (all when ``lang`` is None)."""
    if lang is None:
        return value
    return value.get(lang, value.get("en"))


logger = logging.getLogger("bi_admin.semantic")


def permitted(permission: Permission, user) -> bool:
    """Whether ``user`` passes a dimension/measure ``permission``."""
    if permission is None:
        return True
    if user is None:
        return False
    if isinstance(permission, str):
        permission = [permission]
    if isinstance(permission, list | tuple):
        return bool(user.has_perms(permission))
    try:
        return bool(permission(user))
    except Exception:
        logger.exception("Visibility predicate %r failed; hiding the item", permission)
        return False


def field_refs(expr) -> set[str]:
    """Lookup paths an expression reads (its ``F()``s and plain field names), ignoring
    aggregate ``filter=`` conditions."""
    if isinstance(expr, str):
        return {expr}
    if isinstance(expr, F):
        return {expr.name}
    refs: set[str] = set()
    sources = getattr(expr, "source_expressions", None)
    if sources is None:
        sources = expr.get_source_expressions() if hasattr(expr, "get_source_expressions") else []
    for source in sources:
        if source is not None:
            refs |= field_refs(source)
    return refs


class Dimension:
    """A groupable/filterable attribute: a lookup path or an ORM expression."""

    def __init__(
        self,
        expr: str | BaseExpression,
        type: DimensionType | None = None,
        *,
        label: TextInput = None,
        synonyms: SynonymsInput = None,
        description: TextInput = None,
        format: str | None = None,
        hidden: bool = False,
        permission: Permission = None,
    ):
        if not isinstance(expr, str | BaseExpression):
            raise ImproperlyConfigured(f"Dimension needs a lookup path or expression, not {expr!r}")
        if type is not None and type not in DIMENSION_TYPES:
            raise ImproperlyConfigured(f"Unknown dimension type {type!r}")
        self.expr = expr
        self.type: str | None = type
        self._label = label
        self._synonyms = synonyms
        self._description = description
        self.format = format
        self.hidden = hidden
        self.permission = permission
        # filled by bind()
        self.key = ""
        self.label: Text = {}
        self.synonyms: dict[str, list[str]] = {}
        self.description: Text = {}
        self.steps: list[PathStep] = []
        self.field: FieldInfo | None = None
        self.choices: list[tuple[Any, Text]] | None = None  # (value, {"fa", "en"})
        self.is_date = False  # True for DateField (no time-zone truncation needed)

    @property
    def path(self) -> str | None:
        return self.expr if isinstance(self.expr, str) else None

    @property
    def fans_out(self) -> bool:
        """True when the path crosses a to-many relation (rows may multiply)."""
        return any(s.many for s in self.steps)

    def bind(self, semantic: Semantic, key: str) -> None:
        self.key = key
        model = semantic.model
        defaults = {lang: humanize(key) for lang in LANGS}
        if self.path is not None:
            try:
                self.steps, self.field = get_registry().resolve_lookup(
                    model_label(model), self.path
                )
            except LookupError as exc:
                raise ImproperlyConfigured(f"{semantic.name}.{key}: {exc}") from None
            kind = self.field.kind
            ends_on_relation = bool(self.steps) and len(self.path.split("__")) == len(self.steps)
            if self.field.is_relation or ends_on_relation:
                kind = "fk"
            inferred = _KIND_TO_TYPE.get(kind, "category")
            if self.type is None:
                self.type = inferred
            if self.type == "time" and kind not in ("date", "datetime"):
                raise ImproperlyConfigured(
                    f"{semantic.name}.{key}: time dimension {self.path!r} is not a date/datetime"
                )
            self.is_date = kind == "date"
            from bi_admin.crud.service import django_field, path_label

            defaults = in_languages(lambda: path_label(model, self.path))
            dfield = django_field(model, self.path)
            if kind != "fk" and dfield.choices:
                self.choices = [
                    (value, in_languages(lambda label=label: label))
                    for value, label in dfield.flatchoices
                ]
        else:
            if self.type is None:
                raise ImproperlyConfigured(f"{semantic.name}.{key}: expression needs a type=")
            if self.type == "time":
                out = getattr(self.expr, "_output_field_or_none", None)
                self.is_date = isinstance(out, models.DateField) and not isinstance(
                    out, models.DateTimeField
                )
        self.label = text(self._label, fa=defaults["fa"], en=defaults["en"])
        self.synonyms = synonyms(self._synonyms)
        self.description = text(self._description)

    def catalog(self, lang: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "type": self.type,
            "label": localize(self.label, lang),
            "synonyms": localize(self.synonyms, lang),
            "description": localize(self.description, lang),
        }
        if self.choices:
            out["choices"] = [{"value": v, "label": localize(t, lang)} for v, t in self.choices]
        if self.type == "time":
            out["grains"] = list(TIME_GRAINS)
        if self.fans_out:
            out["fans_out"] = True
        if self.format:
            out["format"] = self.format
        return out


class Measure:
    """A named aggregation with the business logic baked in."""

    def __init__(
        self,
        expr: BaseExpression,
        *,
        label: TextInput = None,
        synonyms: SynonymsInput = None,
        description: TextInput = None,
        format: MeasureFormat = "number",
        hidden: bool = False,
        permission: Permission = None,
    ):
        if not isinstance(expr, BaseExpression) or not expr.contains_aggregate:
            raise ImproperlyConfigured(f"Measure needs an aggregate expression, not {expr!r}")
        if format not in MEASURE_FORMATS:
            raise ImproperlyConfigured(f"Unknown measure format {format!r}")
        self.expr = expr
        self.format = format
        self.hidden = hidden
        self.permission = permission
        self._label = label
        self._synonyms = synonyms
        self._description = description
        self.key = ""
        self.label: Text = {}
        self.synonyms: dict[str, list[str]] = {}
        self.description: Text = {}

    def bind(self, semantic: Semantic, key: str) -> None:
        self.key = key
        try:  # resolves field references without touching the database
            semantic.model._default_manager.annotate(_gozar_check=self.expr).query  # noqa: B018
        except (FieldError, ValueError, TypeError) as exc:
            raise ImproperlyConfigured(f"{semantic.name}.{key}: {exc}") from None
        self.label = text(self._label, fa=humanize(key), en=humanize(key))
        self.synonyms = synonyms(self._synonyms)
        self.description = text(self._description)

    def catalog(self, lang: str | None = None) -> dict[str, Any]:
        return {
            "key": self.key,
            "format": self.format,
            "label": localize(self.label, lang),
            "synonyms": localize(self.synonyms, lang),
            "description": localize(self.description, lang),
        }


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class Semantic:
    """Base class for semantic models. Subclass, then decorate with ``@register``."""

    model: type[models.Model]
    name: str | None = None  # key used in ReportSpec.semantic_model
    label: TextInput = None
    synonyms: SynonymsInput = None
    description: TextInput = None
    time_dimension: str | None = None
    dimensions: dict[str, Dimension] = {}
    measures: dict[str, Measure] = {}
    # Row rules for reports on this model, on top of the model's own (see
    # bi_admin.access.rules): callables taking the user, returning Q/True/False/None.
    row_rules: list = []
    # Run reports as background jobs (None: GOZAR["REPORT_ASYNC"]).
    run_async: bool | None = None
    # Few-shot examples for the NL interface: [{"question": str, "spec": {...}}]. Only
    # examples whose keys the user may see are shown to the model (and offered in the UI).
    nl_examples: list[dict[str, Any]] = []

    def __init__(self):
        cls = type(self)
        if not isinstance(getattr(cls, "model", None), type) or not issubclass(
            cls.model, models.Model
        ):
            raise ImproperlyConfigured(f"{cls.__name__}.model must be a Django model")
        self.name = cls.name or _snake(cls.__name__)
        self.model_label = model_label(cls.model)
        opts = cls.model._meta
        plural = in_languages(lambda: capfirst(opts.verbose_name_plural))
        self.label = text(cls.label, fa=plural["fa"], en=plural["en"])
        self.synonyms = synonyms(cls.synonyms)
        self.description = text(cls.description)
        # Copy declarations so each bound instance owns its Dimension/Measure state.
        self.dimensions = {k: _copy(d) for k, d in cls.dimensions.items()}
        self.measures = {k: _copy(m) for k, m in cls.measures.items()}
        if not self.measures:
            raise ImproperlyConfigured(f"{self.name}: declare at least one measure")
        for key, dim in self.dimensions.items():
            if key in self.measures:
                raise ImproperlyConfigured(f"{self.name}: {key!r} is both a dimension and measure")
            dim.bind(self, key)
        for key, measure in self.measures.items():
            measure.bind(self, key)
        self.time_dimension = cls.time_dimension
        if self.time_dimension is None:
            self.time_dimension = next(
                (k for k, d in self.dimensions.items() if d.type == "time"), None
            )
        elif self.dimensions.get(self.time_dimension, None) is None or (
            self.dimensions[self.time_dimension].type != "time"
        ):
            raise ImproperlyConfigured(
                f"{self.name}.time_dimension {self.time_dimension!r} is not a time dimension"
            )

    # -- hooks --------------------------------------------------------------------

    def _crud_options(self):
        from bi_admin.crud import site

        if self.model_label in get_registry():
            return site.options_for(self.model_label)
        return None

    def get_queryset(self, request) -> models.QuerySet:
        """Base rows for every report; defaults to the CRUD options' queryset."""
        options = self._crud_options()
        if options is not None:
            return options.get_queryset(request)
        return self.model._default_manager.all()

    def has_permission(self, request) -> bool:
        """Who may run reports on this model: by default, whoever may view it."""
        options = self._crud_options()
        if options is not None:
            return options.has_view_permission(request)
        user = request.user
        opts = self.model._meta
        return bool(
            user.is_active
            and user.is_staff
            and user.has_perm(f"{opts.app_label}.view_{opts.model_name}")
        )

    # -- catalog ------------------------------------------------------------------

    def is_visible(self, item: Dimension | Measure, request) -> bool:
        """Whether the user behind ``request`` may see ``item`` (hook). Items they
        can't see behave exactly like unknown keys everywhere."""
        if item.hidden:
            return False
        if request is None:  # trusted code
            return True
        return permitted(item.permission, getattr(request, "user", None))

    def visible_dimensions(self, request=None) -> dict[str, Dimension]:
        return {k: d for k, d in self.dimensions.items() if self.is_visible(d, request)}

    def visible_measures(self, request=None) -> dict[str, Measure]:
        return {k: m for k, m in self.measures.items() if self.is_visible(m, request)}

    def default_time_dimension(self, request=None) -> str | None:
        key = self.time_dimension
        return key if key in self.visible_dimensions(request) else None

    def hidden_fields(self, request=None) -> set[str]:
        """Lookup paths only invisible items expose — kept out of drill-down rows."""
        hidden: set[str] = set()
        shown: set[str] = set()
        for item in [*self.dimensions.values(), *self.measures.values()]:
            if item.hidden:
                continue
            refs = field_refs(item.expr)
            (shown if self.is_visible(item, request) else hidden).update(refs)
        return hidden - shown

    def catalog(self, lang: str | None = None, request=None) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model_label,
            "label": localize(self.label, lang),
            "synonyms": localize(self.synonyms, lang),
            "description": localize(self.description, lang),
            "time_dimension": self.default_time_dimension(request),
            "dimensions": [d.catalog(lang) for d in self.visible_dimensions(request).values()],
            "measures": [m.catalog(lang) for m in self.visible_measures(request).values()],
        }


def _ident(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _copy(obj):
    clone = object.__new__(type(obj))
    clone.__dict__ = dict(obj.__dict__)
    return clone


class SemanticSite:
    """Registered semantic model classes, instantiated (and validated) lazily."""

    def __init__(self):
        self._classes: dict[str, type[Semantic]] = {}
        self._instances: dict[str, Semantic] = {}
        self._lock = threading.Lock()

    def register(self, cls: type[Semantic]) -> type[Semantic]:
        if not (isinstance(cls, type) and issubclass(cls, Semantic)):
            raise ImproperlyConfigured(f"@register expects a Semantic subclass, got {cls!r}")
        name = cls.name or _snake(cls.__name__)
        existing = self._classes.get(name)
        if existing is not None and _ident(existing) != _ident(cls):
            raise ImproperlyConfigured(f"Semantic model {name!r} is already registered")
        self._classes[name] = cls
        self._instances.pop(name, None)
        return cls

    def unregister(self, name: str) -> None:
        self._classes.pop(name, None)
        self._instances.pop(name, None)

    def names(self) -> list[str]:
        return list(self._classes)

    def __contains__(self, name: str) -> bool:
        return name in self._classes

    def get(self, name: str) -> Semantic:
        if name not in self._classes:
            raise LookupError(f"Unknown semantic model {name!r}")
        if name not in self._instances:
            with self._lock:
                if name not in self._instances:
                    self._instances[name] = self._classes[name]()
        return self._instances[name]

    def all(self) -> list[Semantic]:
        return [self.get(name) for name in self._classes]

    def clear_cache(self) -> None:
        self._instances.clear()


semantic_site = SemanticSite()


def register(cls: type[Semantic]) -> type[Semantic]:
    """Class decorator registering a semantic model (``@register``)."""
    return semantic_site.register(cls)


def catalog(lang: str | None = None, request=None) -> list[dict[str, Any]]:
    """The semantic catalog — every registered model (only those ``request.user``
    may report on, when given) with labels in ``lang`` (both when ``None``)."""
    out = []
    for semantic in semantic_site.all():
        if request is not None and not semantic.has_permission(request):
            continue
        out.append(semantic.catalog(lang, request))
    return out
