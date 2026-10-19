"""Per-model CRUD options — the Gozar counterpart of ``ModelAdmin``.

Every discovered model gets sensible defaults; a project overrides them with::

    from bi_admin.crud import ModelOptions, register

    @register(Order)
    class OrderOptions(ModelOptions):
        list_display = ["__str__", "customer", "customer__city__name", "status", "amount"]
        search_fields = ["customer__name", "=id"]
        list_filter = ["status", "created_at", "customer__city"]
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.db import models
from django.utils.translation import gettext as _

from bi_admin.discovery import ModelInfo, get_registry, model_label

AUTO_LIST_COLUMNS = 6
SEARCHABLE_KINDS = {"string", "email", "url"}
FILTERABLE_KINDS = {"boolean", "date", "datetime", "fk", "o2o"}
UNLISTABLE_KINDS = {"text", "json", "m2m", "file"}


@dataclass(frozen=True)
class Action:
    """A bulk action on selected rows (like admin actions)."""

    name: str
    label: str
    handler: Callable[[Any, models.QuerySet], dict | None]
    permission: str = "change"  # view | add | change | delete
    destructive: bool = False


def action(label: str, permission: str = "change", destructive: bool = False):
    """Decorator turning ``fn(options, request, queryset)`` into an ``Action``."""

    def wrap(fn):
        fn.gozar_action = {"label": label, "permission": permission, "destructive": destructive}
        return fn

    return wrap


class ModelOptions:
    """Declarative options; any attribute left as ``None`` is derived from ``_meta``."""

    list_display: list[str] | None = None
    list_display_links: list[str] | None = None
    search_fields: list[str] | None = None
    list_filter: list[str] | None = None
    ordering: list[str] | None = None
    list_per_page: int = 50
    list_max_page_size: int = 500
    fields: list[str] | None = None  # editable form fields
    exclude: list[str] = []
    readonly_fields: list[str] = []
    password_fields: list[str] = []  # write-only; hashed via set_password()
    actions: list[str] = []  # names of methods decorated with @action
    show_in_menu: bool = True
    # Row-level rules (bi_admin.access.rules): callables taking the user and returning
    # a Q (rows they may see and touch), True/None (no restriction) or False (none).
    row_rules: list = []

    def __init__(self, model: type[models.Model]):
        self.model = model
        self.label = model_label(model)

    # -- metadata -----------------------------------------------------------------

    @property
    def info(self) -> ModelInfo:
        return get_registry().get(self.label)

    def _visible_fields(self):
        hidden = set(self.exclude) | set(self.password_fields)
        return [f for f in self.info.fields.values() if f.name not in hidden]

    def get_list_display(self) -> list[str]:
        if self.list_display is not None:
            return list(self.list_display)
        cols = ["__str__"]
        for f in self._visible_fields():
            if f.primary_key or f.kind in UNLISTABLE_KINDS:
                continue
            cols.append(f.name)
            if len(cols) > AUTO_LIST_COLUMNS:
                break
        return cols

    def get_list_display_links(self) -> list[str]:
        if self.list_display_links is not None:
            return list(self.list_display_links)
        return self.get_list_display()[:1]

    def get_search_fields(self) -> list[str]:
        if self.search_fields is not None:
            return list(self.search_fields)
        return [
            f.name for f in self._visible_fields() if f.kind in SEARCHABLE_KINDS and not f.choices
        ]

    def get_list_filter(self) -> list[str]:
        if self.list_filter is not None:
            return list(self.list_filter)
        return [
            f.name
            for f in self._visible_fields()
            if not f.primary_key and (f.choices or f.kind in FILTERABLE_KINDS)
        ]

    def get_ordering(self) -> list[str]:
        if self.ordering is not None:
            return list(self.ordering)
        return self.info.ordering or [f"-{self.info.pk_name}"]

    def get_fields(self) -> list[str]:
        """Fields shown on the detail form (editable + read-only), in order."""
        if self.fields is not None:
            names = list(self.fields)
        else:
            names = [f.name for f in self._visible_fields() if not f.primary_key]
            names += [p for p in self.password_fields if p not in names]
        return [n for n in names if n not in self.exclude]

    def get_readonly_fields(self) -> list[str]:
        info = self.info
        auto = [
            n
            for n in self.get_fields()
            if n in info.fields and (not info.fields[n].editable or info.fields[n].kind == "file")
        ]
        auto += self.reverse_relations()  # listed for reading only
        return list(dict.fromkeys([*self.readonly_fields, *auto]))

    def reverse_relations(self) -> list[str]:
        """Names in ``fields`` that are reverse relations (``orders`` on Customer)."""
        out = []
        for name in self.fields or []:
            try:
                f = self.model._meta.get_field(name)
            except Exception:
                continue
            if f.is_relation and f.auto_created and not f.concrete:
                out.append(name)
        return out

    def get_form_fields(self) -> list[str]:
        ro = set(self.get_readonly_fields()) | set(self.password_fields)
        return [n for n in self.get_fields() if n not in ro]

    def get_actions(self) -> dict[str, Action]:
        out = {
            "delete_selected": Action(
                "delete_selected", _("Delete selected"), _delete_selected, "delete", True
            )
        }
        for name in self.actions:
            fn = getattr(self, name)
            meta = getattr(fn, "gozar_action", {"label": name})
            out[name] = Action(
                name,
                str(meta.get("label", name)),
                lambda request, qs, fn=fn: fn(request, qs),
                meta.get("permission", "change"),
                meta.get("destructive", False),
            )
        return out

    # -- hooks --------------------------------------------------------------------

    def get_queryset(self, request) -> models.QuerySet:
        return self.model._default_manager.all()

    def has_perm(self, request, perm: str) -> bool:
        user = request.user
        if not (user.is_active and user.is_staff):
            return False
        opts = self.model._meta
        codename = f"{opts.app_label}.{perm}_{opts.model_name}"
        if perm == "view":
            return user.has_perm(codename) or user.has_perm(
                f"{opts.app_label}.change_{opts.model_name}"
            )
        return user.has_perm(codename)

    def has_view_permission(self, request) -> bool:
        return self.has_perm(request, "view")

    def has_add_permission(self, request) -> bool:
        return self.has_perm(request, "add")

    def has_change_permission(self, request, obj=None) -> bool:
        return self.has_perm(request, "change")

    def has_delete_permission(self, request, obj=None) -> bool:
        return self.has_perm(request, "delete")

    def permissions(self, request) -> dict[str, bool]:
        return {
            "view": self.has_view_permission(request),
            "add": self.has_add_permission(request),
            "change": self.has_change_permission(request),
            "delete": self.has_delete_permission(request),
        }

    def save_model(self, request, obj: models.Model, form, change: bool) -> None:
        obj.save()

    def delete_model(self, request, obj: models.Model) -> None:
        obj.delete()


def _delete_selected(request, queryset):
    from bi_admin.crud.service import delete_queryset

    return delete_queryset(request, queryset)


class UserOptions(ModelOptions):
    list_display = ["username", "email", "first_name", "last_name", "is_staff", "is_active"]
    search_fields = ["username", "email", "first_name", "last_name"]
    list_filter = ["is_staff", "is_superuser", "is_active", "groups"]
    ordering = ["username"]
    fields = [
        "username",
        "password",
        "first_name",
        "last_name",
        "email",
        "is_active",
        "is_staff",
        "is_superuser",
        "groups",
        "user_permissions",
        "last_login",
        "date_joined",
    ]
    readonly_fields = ["last_login", "date_joined"]
    password_fields = ["password"]


class GroupOptions(ModelOptions):
    search_fields = ["name"]
    list_filter = []
    ordering = ["name"]


class Site:
    """Holds the ``ModelOptions`` instance for each registered model label."""

    def __init__(self):
        self._options: dict[str, type[ModelOptions]] = {}
        self._cache: dict[str, ModelOptions] = {}

    def register(self, model_or_models, options_class: type[ModelOptions] | None = None):
        if isinstance(model_or_models, type) and issubclass(model_or_models, models.Model):
            targets: Iterable = [model_or_models]
        else:
            targets = model_or_models
        for m in targets:
            label = model_label(m) if not isinstance(m, str) else m.lower()
            self._options[label] = options_class or ModelOptions
            self._cache.pop(label, None)

    def options_for(self, label: str) -> ModelOptions:
        label = label.lower()
        if label not in self._cache:
            get_registry().get(label)  # raises LookupError when not discoverable
            cls = self._options.get(label) or _DEFAULTS.get(label, ModelOptions)
            self._cache[label] = cls(apps.get_model(label))
        return self._cache[label]

    def clear_cache(self) -> None:
        self._cache.clear()


_DEFAULTS: dict[str, type[ModelOptions]] = {"auth.user": UserOptions, "auth.group": GroupOptions}

site = Site()


def register(*models_: type[models.Model]):
    """Class decorator: ``@register(Order)`` binds a ``ModelOptions`` subclass."""

    def wrap(cls: type[ModelOptions]):
        site.register(list(models_), cls)
        return cls

    return wrap
