"""Per-model CRUD options — the Gozar counterpart of ``ModelAdmin``.

Every discovered model gets sensible defaults; a project overrides them with::

    from bi_admin.crud import ModelOptions, TabularInline, register

    class ItemInline(TabularInline):
        model = OrderItem
        extra = 1
        autocomplete_fields = ["product"]

    @register(Order)
    class OrderOptions(ModelOptions):
        list_display = ["__str__", "customer", "customer__city__name", "status", "amount"]
        list_editable = ["status"]
        search_fields = ["customer__name", "=id"]
        list_filter = ["status", "created_at", "customer__city"]
        date_hierarchy = "created_at"
        inlines = [ItemInline]

The names follow ``django.contrib.admin`` so existing admin classes port over with
few changes; ``check()`` reports configuration mistakes as system checks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.forms.models import _get_foreign_key
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from bi_admin.discovery import ModelInfo, build_model_info, get_registry, model_label

AUTO_LIST_COLUMNS = 6
SEARCHABLE_KINDS = {"string", "email", "url"}
FILTERABLE_KINDS = {"boolean", "date", "datetime", "fk", "o2o"}
UNLISTABLE_KINDS = {"text", "json", "m2m", "file"}
# Widget names the SPA renders (``ModelOptions.widgets`` values).
WIDGETS = {
    "input",
    "textarea",
    "select",
    "radio",
    "rich_select",
    "checkbox",
    "autocomplete",
    "raw_id",
    "dual_list",
    "chips",
    "permissions",
    "password",
    "file",
    "image",
}


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


def display(function=None, *, description=None, boolean=None, ordering=None, empty_value=None):
    """Like ``django.contrib.admin.display``: label, boolean icon, sort path and empty
    text of a ``list_display`` / ``readonly_fields`` callable. The attribute names
    are admin's, so callables decorated with ``@admin.display`` work too."""

    def decorator(fn):
        if description is not None:
            fn.short_description = description
        if boolean is not None:
            fn.boolean = boolean
        if ordering is not None:
            fn.admin_order_field = ordering
        if empty_value is not None:
            fn.empty_value_display = empty_value
        return fn

    return decorator if function is None else decorator(function)


def normalize_fieldsets(fieldsets) -> list[dict[str, Any]]:
    """Admin-style ``[(title, {"fields": [...], "classes": [...], "description": ...})]``
    as ``[{"title", "description", "collapse", "classes", "rows": [[name, ...], ...]}]``.
    A tuple inside ``fields`` puts its fields on one line."""
    out = []
    for title, opts in fieldsets:
        rows = []
        for entry in opts.get("fields", ()):
            names = list(entry) if isinstance(entry, list | tuple) else [entry]
            rows.append([_name_of(n) for n in names])
        classes = list(opts.get("classes", ()))
        out.append(
            {
                "title": str(title) if title else None,
                "description": str(opts.get("description") or ""),
                "classes": classes,
                "collapse": "collapse" in classes,
                "rows": rows,
            }
        )
    return out


def _name_of(entry) -> str:
    return entry if isinstance(entry, str) else entry.__name__


def flatten_fieldsets(fieldsets) -> list[str]:
    return [name for fs in normalize_fieldsets(fieldsets) for row in fs["rows"] for name in row]


class FormOptionsMixin:
    """Form layout options shared by ``ModelOptions`` and inlines."""

    model: type[models.Model]
    fields: list | None = None
    exclude: list[str] = []
    readonly_fields: list = []  # field names, or callables/method names (computed values)
    widgets: dict[str, str] = {}  # field → a name in WIDGETS
    radio_fields: dict[str, str] = {}  # field → "horizontal" | "vertical"
    raw_id_fields: list[str] = []
    autocomplete_fields: list[str] = []
    filter_horizontal: list[str] = []  # many-to-many fields with a dual-list selector
    filter_vertical: list[str] = []
    form = None  # a ModelForm subclass (its clean() runs); fields still come from options

    @property
    def info(self) -> ModelInfo:
        label = model_label(self.model)
        if label in get_registry():
            return get_registry().get(label)
        return build_model_info(self.model)

    def _hidden(self) -> set[str]:
        return set(self.exclude)

    def _visible_fields(self):
        hidden = self._hidden()
        return [f for f in self.info.fields.values() if f.name not in hidden]

    def computed_fields(self) -> dict[str, Callable]:
        """Read-only entries that aren't model fields: callables, methods on these
        options (taking the object) or attributes/methods of the model."""
        out: dict[str, Callable] = {}
        for entry in self.readonly_fields:
            if callable(entry):
                out[entry.__name__] = entry
                continue
            if entry in self.info.fields or self._is_relation_name(entry):
                continue
            fn = None if entry.startswith("__") else getattr(self, entry, None)  # not __str__
            if callable(fn):
                out[entry] = fn
            else:
                out[entry] = _model_attr(entry)
        return out

    def _is_relation_name(self, name: str) -> bool:
        try:
            self.model._meta.get_field(name)
        except FieldDoesNotExist:
            return False
        return True

    def get_readonly_names(self) -> list[str]:
        return [_name_of(e) for e in self.readonly_fields]

    def widget_for(self, name: str) -> dict[str, Any]:
        """``{"type": ..., ...}`` — how the SPA renders a form field."""
        if name in self.widgets:
            return {"type": self.widgets[name]}
        if name in self.radio_fields:
            return {"type": "radio", "orientation": self.radio_fields[name]}
        if name in self.raw_id_fields:
            return {"type": "raw_id"}
        if name in self.autocomplete_fields:
            return {"type": "autocomplete"}
        if name in self.filter_horizontal or name in self.filter_vertical:
            vertical = name in self.filter_vertical
            return {"type": "dual_list", "orientation": "vertical" if vertical else "horizontal"}
        f = self.info.fields.get(name)
        if f is None:
            return {"type": "input"}
        if f.choices:
            return {"type": "select"}
        if f.image:
            return {"type": "image"}
        return {"type": _DEFAULT_WIDGETS.get(f.kind, "input")}

    def check_form_options(self, where: str) -> list[str]:
        errors = []
        names = set(self.info.fields)
        for attr in ("raw_id_fields", "autocomplete_fields", "filter_horizontal"):
            for name in getattr(self, attr):
                if name not in names:
                    errors.append(f"{where}.{attr}: {name!r} is not a field of the model")
        for name, widget in self.widgets.items():
            if widget not in WIDGETS:
                errors.append(f"{where}.widgets[{name!r}]: unknown widget {widget!r}")
        for name in (*self.filter_horizontal, *self.filter_vertical):
            if name in names and self.info.fields[name].kind != "m2m":
                errors.append(f"{where}.filter_horizontal: {name!r} is not a many-to-many field")
        for name in (*self.raw_id_fields, *self.autocomplete_fields):
            if name in names and not self.info.fields[name].is_relation:
                errors.append(f"{where}: {name!r} is not a relation field")
        return errors


_DEFAULT_WIDGETS = {
    "text": "textarea",
    "json": "textarea",
    "boolean": "checkbox",
    "fk": "autocomplete",
    "o2o": "autocomplete",
    "m2m": "chips",
    "file": "file",
}


def _model_attr(name: str) -> Callable:
    def read(obj):
        value = getattr(obj, name)
        return value() if callable(value) else value

    read.__name__ = name
    return read


class ModelOptions(FormOptionsMixin):
    """Declarative options; any attribute left as ``None`` is derived from ``_meta``."""

    # -- list -------------------------------------------------------------------------
    list_display: list | None = None  # fields, lookup paths, callables or method names
    list_display_links: list[str] | None = None
    list_editable: list[str] = []  # edited in place on the list (change permission)
    search_fields: list[str] | None = None
    search_help_text: str | None = None
    list_filter: list[str] | None = None
    date_hierarchy: str | None = None  # a date/datetime field: year → month → day drill-down
    ordering: list[str] | None = None
    sortable_by: list[str] | None = None  # None: every sortable column
    list_per_page: int = 50
    list_max_page_size: int = 500
    show_full_result_count: bool = True  # False skips the unfiltered COUNT(*)
    list_select_related: bool | list[str] = False  # False: derived from list_display
    empty_value_display: str = "—"
    actions: list[str] = []  # names of methods decorated with @action
    bulk_edit_fields: list[str] | None = None  # None: the form fields that make sense
    import_fields: list[str] | None = None  # None: the form fields
    show_in_menu: bool = True
    # -- form -------------------------------------------------------------------------
    fieldsets: list | None = None
    add_fieldsets: list | None = None  # the add form's layout (default: ``fieldsets``)
    add_form = None  # a form class for adding (e.g. UserCreationForm with two passwords)
    change_password_form = None  # a SetPasswordForm-like class: enables the password page
    password_fields: list[str] = []  # write-only; hashed via set_password()
    prepopulated_fields: dict[str, list[str]] = {}  # e.g. {"slug": ["name"]}, on add
    inlines: list[type[InlineOptions]] = []
    save_as: bool = False  # "Save as new" on the change form
    save_as_continue: bool = True  # after "save as new", stay on the new object
    save_on_top: bool = False  # the save buttons above the form as well
    view_on_site: bool | Callable = True  # link to get_absolute_url() (or fn(obj) → url)
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

    def _hidden(self) -> set[str]:
        return set(self.exclude) | set(self.password_fields)

    def get_list_display(self) -> list[str]:
        if self.list_display is not None:
            return [_name_of(c) for c in self.list_display]
        cols = ["__str__"]
        for f in self._visible_fields():
            if f.primary_key or f.kind in UNLISTABLE_KINDS:
                continue
            cols.append(f.name)
            if len(cols) > AUTO_LIST_COLUMNS:
                break
        return cols

    def list_callable(self, name: str) -> Callable | None:
        """The callable behind a non-field ``list_display`` entry."""
        for entry in self.list_display or ():
            if callable(entry) and entry.__name__ == name:
                return entry
        fn = getattr(self, name, None)
        if callable(fn):
            return fn
        attr = getattr(self.model, name, None)
        if attr is None:
            return None
        if isinstance(attr, property):
            return attr.fget
        return attr if callable(attr) else None

    def get_list_display_links(self) -> list[str]:
        if self.list_display_links is not None:
            return list(self.list_display_links)
        return [c for c in self.get_list_display() if c not in self.list_editable][:1]

    def get_list_editable(self) -> list[str]:
        return list(self.list_editable)

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

    def get_fieldsets(self, add: bool = False) -> list[dict[str, Any]]:
        """The form layout: declared fieldsets, else one untitled set of every field."""
        if add and self.add_fieldsets is not None:
            return normalize_fieldsets(self.add_fieldsets)
        if self.fieldsets is not None:
            return normalize_fieldsets(self.fieldsets)
        rows = [[n] for n in (self.get_add_fields() if add else self.get_fields())]
        return [{"title": None, "description": "", "classes": [], "collapse": False, "rows": rows}]

    def get_fields(self) -> list[str]:
        """Fields shown on the change form (editable + read-only), in order."""
        if self.fieldsets is not None:
            names = flatten_fieldsets(self.fieldsets)
        elif self.fields is not None:
            names = [_name_of(n) for n in self.fields]
        else:
            names = [f.name for f in self._visible_fields() if not f.primary_key]
            names += [p for p in self.password_fields if p not in names]
            names += [n for n in self.get_readonly_names() if n not in names]
        return [n for n in names if n not in self.exclude]

    def get_add_fields(self) -> list[str]:
        """Fields of the add form (``add_fieldsets``, else the change form's)."""
        if self.add_fieldsets is not None:
            return flatten_fieldsets(self.add_fieldsets)
        return self.get_fields()

    def get_readonly_fields(self) -> list[str]:
        info = self.info
        auto = [n for n in self.get_fields() if n in info.fields and not info.fields[n].editable]
        auto += self.reverse_relations()  # listed for reading only
        auto += list(self.computed_fields())
        return list(dict.fromkeys([*self.get_readonly_names(), *auto]))

    def reverse_relations(self) -> list[str]:
        """Names in ``fields`` that are reverse relations (``orders`` on Customer)."""
        out = []
        for name in self.get_fields():
            try:
                f = self.model._meta.get_field(name)
            except FieldDoesNotExist:
                continue
            if f.is_relation and f.auto_created and not f.concrete:
                out.append(name)
        return out

    def get_form_fields(self) -> list[str]:
        ro = set(self.get_readonly_fields()) | set(self.password_fields)
        return [n for n in self.get_fields() if n not in ro]

    def get_bulk_edit_fields(self) -> list[str]:
        """Fields offered by bulk edit: editable, not unique, not files or passwords."""
        if self.bulk_edit_fields is not None:
            return list(self.bulk_edit_fields)
        fields = self.info.fields
        return [
            n
            for n in self.get_form_fields()
            if n in fields and not fields[n].unique and fields[n].kind not in ("file",)
        ]

    def get_import_fields(self) -> list[str]:
        if self.import_fields is not None:
            return list(self.import_fields)
        fields = self.info.fields
        return [n for n in self.get_form_fields() if n in fields and fields[n].kind != "file"]

    def get_inlines(self) -> list[InlineOptions]:
        return [cls(self.model) for cls in self.inlines]

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

    def get_view_on_site_url(self, obj) -> str | None:
        if obj is None or obj.pk is None or not self.view_on_site:
            return None
        if callable(self.view_on_site):
            return self.view_on_site(obj)
        method = getattr(obj, "get_absolute_url", None)
        return method() if callable(method) else None

    # -- configuration checks -----------------------------------------------------

    def check(self) -> list[str]:
        """Configuration mistakes (reported as system check ``bi_admin.E005``)."""
        where = type(self).__name__
        errors = self.check_form_options(where)
        fields = self.info.fields
        display = self.get_list_display()
        links = set(self.get_list_display_links())
        for name in self.list_editable:
            if name not in display:
                errors.append(f"{where}.list_editable: {name!r} is not in list_display")
            elif name in links:
                errors.append(f"{where}.list_editable: {name!r} is also in list_display_links")
            elif name not in fields or not fields[name].editable:
                errors.append(f"{where}.list_editable: {name!r} is not an editable field")
        if self.date_hierarchy:
            try:
                _steps, finfo = get_registry().resolve_lookup(self.label, self.date_hierarchy)
            except LookupError:
                errors.append(f"{where}.date_hierarchy: unknown field {self.date_hierarchy!r}")
            else:
                if finfo.kind not in ("date", "datetime"):
                    errors.append(f"{where}.date_hierarchy: {self.date_hierarchy!r} is not a date")
        for target, sources in self.prepopulated_fields.items():
            if target not in fields:
                errors.append(f"{where}.prepopulated_fields: unknown field {target!r}")
            for source in sources:
                if source not in fields:
                    errors.append(f"{where}.prepopulated_fields: unknown field {source!r}")
        for inline in self.get_inlines():
            errors += inline.check(f"{where}.inlines[{type(inline).__name__}]")
        return errors

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

    def has_export_permission(self, request) -> bool:
        return self.has_view_permission(request)

    def has_import_permission(self, request) -> bool:
        return self.has_add_permission(request) or self.has_change_permission(request)

    def permissions(self, request) -> dict[str, bool]:
        return {
            "view": self.has_view_permission(request),
            "add": self.has_add_permission(request),
            "change": self.has_change_permission(request),
            "delete": self.has_delete_permission(request),
            "export": self.has_export_permission(request),
            "import": self.has_import_permission(request),
        }

    def validate_save(self, request, form, obj=None) -> dict[str, list[str]]:
        """Extra checks on a valid form before saving: ``{field: [messages]}``."""
        return {}

    def save_model(self, request, obj: models.Model, form, change: bool) -> None:
        obj.save()

    def save_formset(self, request, form, formset, change: bool) -> None:
        formset.save()

    def delete_model(self, request, obj: models.Model) -> None:
        obj.delete()


class InlineOptions(FormOptionsMixin):
    """Related rows edited on the parent's form (admin's ``InlineModelAdmin``).

    Saved with the parent in one transaction; every row is validated by its own
    form and the formset (``min_num``/``max_num``, unique checks across rows)."""

    model: type[models.Model]
    fk_name: str | None = None
    name: str | None = None  # key in the API payload; default: the reverse accessor
    style = "tabular"  # or "stacked"
    extra = 3  # empty rows offered on the form
    min_num: int | None = None
    max_num: int | None = None
    can_delete = True
    show_change_link = False  # link each row to its own change page (when registered)
    verbose_name: str | None = None
    verbose_name_plural: str | None = None
    ordering: list[str] | None = None

    def __init__(self, parent_model: type[models.Model]):
        self.parent_model = parent_model
        self.fk = _get_foreign_key(parent_model, self.model, fk_name=self.fk_name)
        self.label = model_label(self.model)

    def get_name(self) -> str:
        return self.name or self.fk.remote_field.get_accessor_name()

    def get_fields(self) -> list[str]:
        if self.fields is not None:
            names = [_name_of(n) for n in self.fields]
        else:
            names = [
                f.name
                for f in self._visible_fields()
                if not f.primary_key and f.name != self.fk.name
            ]
            names += [n for n in self.get_readonly_names() if n not in names]
        return [n for n in names if n not in self.exclude and n != self.fk.name]

    def get_readonly_fields(self) -> list[str]:
        info = self.info
        auto = [n for n in self.get_fields() if n in info.fields and not info.fields[n].editable]
        auto += list(self.computed_fields())
        return list(dict.fromkeys([*self.get_readonly_names(), *auto]))

    def get_form_fields(self) -> list[str]:
        ro = set(self.get_readonly_fields())
        return [n for n in self.get_fields() if n not in ro]

    def get_ordering(self) -> list[str]:
        if self.ordering is not None:
            return list(self.ordering)
        return list(self.model._meta.ordering or []) or ["pk"]

    def get_queryset(self, request) -> models.QuerySet:
        return self.model._default_manager.all()

    def check(self, where: str) -> list[str]:
        return self.check_form_options(where)

    # -- permissions (like admin: the inline model's perms; auto-created through
    #    models of a many-to-many follow the parent's change permission) ------------

    def _perm(self, request, perm: str) -> bool:
        user = request.user
        if not (user.is_active and user.is_staff):
            return False
        opts = self.model._meta
        if opts.auto_created:
            parent = self.parent_model._meta
            if perm == "view":
                return user.has_perm(f"{parent.app_label}.view_{parent.model_name}") or (
                    user.has_perm(f"{parent.app_label}.change_{parent.model_name}")
                )
            return user.has_perm(f"{parent.app_label}.change_{parent.model_name}")
        if perm == "view":
            return user.has_perm(f"{opts.app_label}.view_{opts.model_name}") or user.has_perm(
                f"{opts.app_label}.change_{opts.model_name}"
            )
        return user.has_perm(f"{opts.app_label}.{perm}_{opts.model_name}")

    def has_view_permission(self, request, obj=None) -> bool:
        return self._perm(request, "view")

    def has_add_permission(self, request, obj=None) -> bool:
        return self._perm(request, "add")

    def has_change_permission(self, request, obj=None) -> bool:
        return self._perm(request, "change")

    def has_delete_permission(self, request, obj=None) -> bool:
        return self.can_delete and self._perm(request, "delete")

    def permissions(self, request, obj=None) -> dict[str, bool]:
        return {
            "view": self.has_view_permission(request, obj),
            "add": self.has_add_permission(request, obj),
            "change": self.has_change_permission(request, obj),
            "delete": self.has_delete_permission(request, obj),
        }


class TabularInline(InlineOptions):
    style = "tabular"


class StackedInline(InlineOptions):
    style = "stacked"


def _delete_selected(request, queryset):
    from bi_admin.crud.service import delete_queryset

    return delete_queryset(request, queryset)


def _user_options():
    from django.contrib.auth.forms import AdminPasswordChangeForm, AdminUserCreationForm

    class UserOptions(ModelOptions):
        list_display = ["username", "email", "first_name", "last_name", "is_staff", "is_active"]
        search_fields = ["username", "email", "first_name", "last_name"]
        list_filter = ["is_staff", "is_superuser", "is_active", "groups"]
        ordering = ["username"]
        fieldsets = [
            (None, {"fields": ["username", "password"]}),
            (gettext_lazy("Personal info"), {"fields": [("first_name", "last_name"), "email"]}),
            (
                gettext_lazy("Permissions"),
                {"fields": ["is_active", "is_staff", "is_superuser", "groups", "user_permissions"]},
            ),
            (gettext_lazy("Important dates"), {"fields": ["last_login", "date_joined"]}),
        ]
        add_fieldsets = [
            (
                None,
                {
                    "fields": ["username", "usable_password", "password1", "password2"],
                    "description": gettext_lazy(
                        "First, enter a username and password. Then you'll be able to edit "
                        "more user options."
                    ),
                },
            )
        ]
        add_form = AdminUserCreationForm
        change_password_form = AdminPasswordChangeForm
        readonly_fields = ["last_login", "date_joined"]
        password_fields = ["password"]
        filter_horizontal = ["groups"]
        widgets = {"user_permissions": "permissions"}

        def check(self):
            # the stock layout names the password and its add-form fields
            return []

        # Only superusers manage superusers: anyone else could otherwise take over
        # an account with more rights than their own (e.g. by changing its password).
        def has_change_permission(self, request, obj=None) -> bool:
            if obj is not None and obj.is_superuser and not request.user.is_superuser:
                return False
            return super().has_change_permission(request, obj)

        def has_delete_permission(self, request, obj=None) -> bool:
            if obj is not None and obj.is_superuser and not request.user.is_superuser:
                return False
            return super().has_delete_permission(request, obj)

        def validate_save(self, request, form, obj=None):
            return grant_errors(request, form, obj)

    return UserOptions


def _group_options():
    class GroupOptions(ModelOptions):
        search_fields = ["name"]
        list_filter = []
        ordering = ["name"]
        widgets = {"permissions": "permissions"}

        def validate_save(self, request, form, obj=None):
            return grant_errors(request, form, obj)

    return GroupOptions


def grant_errors(request, form, obj=None) -> dict[str, list[str]]:
    """A user who isn't a superuser can't grant what they don't have: superuser
    status, permissions they lack, or groups carrying permissions they lack."""
    user = request.user
    if user.is_superuser:
        return {}
    data = getattr(form, "cleaned_data", {})
    errors: dict[str, list[str]] = {}
    # the form has already written its values onto ``obj``: compare with the stored row
    was_superuser = bool(
        obj is not None
        and obj.pk is not None
        and type(obj)._base_manager.filter(pk=obj.pk, is_superuser=True).exists()
    )
    if data.get("is_superuser") and not was_superuser:
        errors["is_superuser"] = [_("Only superusers can grant superuser status.")]

    def missing(perms) -> list[str]:
        return sorted(
            f"{p.content_type.app_label}.{p.codename}"
            for p in perms
            if not user.has_perm(f"{p.content_type.app_label}.{p.codename}")
        )

    for name in ("user_permissions", "permissions"):
        if name not in data:
            continue
        current = set(getattr(obj, name).values_list("pk", flat=True)) if obj and obj.pk else set()
        added = [p for p in data[name] if p.pk not in current]
        lacking = missing(added)
        if lacking:
            errors[name] = [
                _("You can't grant permissions you don't have: %(perms)s")
                % {"perms": ", ".join(lacking)}
            ]
    if "groups" in data:
        current = set(obj.groups.values_list("pk", flat=True)) if obj and obj.pk else set()
        for group in data["groups"]:
            if group.pk in current:
                continue
            lacking = missing(group.permissions.select_related("content_type"))
            if lacking:
                errors.setdefault("groups", []).append(
                    _("You can't add the group “%(group)s”: it has permissions you don't have.")
                    % {"group": group.name}
                )
    return errors


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
            cls = self._options.get(label) or _default_options(label)
            self._cache[label] = cls(apps.get_model(label))
        return self._cache[label]

    def registered(self) -> dict[str, type[ModelOptions]]:
        return dict(self._options)

    def clear_cache(self) -> None:
        self._cache.clear()


def _default_options(label: str) -> type[ModelOptions]:
    if label == "auth.user":
        return _user_options()
    if label == "auth.group":
        return _group_options()
    return ModelOptions


site = Site()


def register(*models_: type[models.Model]):
    """Class decorator: ``@register(Order)`` binds a ``ModelOptions`` subclass."""

    def wrap(cls: type[ModelOptions]):
        site.register(list(models_), cls)
        return cls

    return wrap
