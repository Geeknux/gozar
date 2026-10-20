from django.utils.encoding import force_str
from django.utils.text import capfirst
from django.utils.translation import gettext as _
from ninja import Router

from bi_admin.crud import inlines, service, users
from bi_admin.crud.forms import form_field_meta
from bi_admin.discovery import get_registry

router = Router(tags=["meta"])


@router.get("/models")
def models_index(request):
    """Apps and models the current user may see, with their permissions."""
    apps: dict[str, dict] = {}
    for opts in service.visible_models(request):
        info = opts.info
        app = apps.setdefault(
            info.app_label,
            {"app_label": info.app_label, "verbose_name": info.app_verbose_name, "models": []},
        )
        app["models"].append(
            {
                "label": info.label,
                "model_name": info.model_name,
                "verbose_name": info.verbose_name,
                "verbose_name_plural": info.verbose_name_plural,
                "permissions": opts.permissions(request),
            }
        )
    return {"apps": list(apps.values())}


@router.get("/models/{app_label}/{model_name}")
def model_detail(request, app_label: str, model_name: str):
    """Everything the UI needs to render list + form for one model."""
    opts = service.get_options(f"{app_label}.{model_name}")
    service.require(opts, request, "view")
    info = opts.info.as_dict()
    hidden = set(opts.exclude)
    info["fields"] = [f for f in info["fields"] if f["name"] not in hidden]
    for name in opts.reverse_relations():  # read-only lists of related rows
        rel = opts.model._meta.get_field(name)
        target = rel.related_model._meta
        info["fields"].append(
            {
                "name": name,
                "verbose_name": capfirst(force_str(target.verbose_name_plural)),
                "kind": "o2o" if rel.one_to_one else "m2m",
                "primary_key": False,
                "editable": False,
                "null": True,
                "blank": True,
                "required": False,
                "max_length": None,
                "decimal_places": None,
                "help_text": "",
                "choices": None,
                "related_model": f"{target.app_label}.{target.model_name}",
            }
        )
    for f in info["fields"]:
        if f["name"] in opts.password_fields:
            f["kind"] = "password"
            f["required"] = False
    names = {f["name"] for f in info["fields"]}
    for name, fn in opts.computed_fields().items():
        if name not in names:
            info["fields"].append(inlines.computed_meta(name, fn))
    add_fields = opts.get_add_fields()
    if opts.add_form is not None:
        for name, ffield in opts.add_form.base_fields.items():
            if name not in names and name in add_fields:
                info["fields"].append(form_field_meta(name, ffield))
    form_names = set(opts.get_fields()) | set(add_fields)
    widgets = {n: opts.widget_for(n) for n in form_names}
    for f in info["fields"]:
        if f.get("radio") and f["name"] not in opts.widgets:
            widgets[f["name"]] = {"type": "radio", "orientation": "vertical"}
    columns = service.list_columns(opts, service.available_columns(opts))
    dh = opts.date_hierarchy
    return {
        **info,
        "permissions": opts.permissions(request),
        "list": {
            "columns": [c.as_dict() for c in service.list_columns(opts)],
            "available_columns": [{"name": c.name, "label": c.label} for c in columns],
            "search_fields": opts.get_search_fields(),
            "search_help_text": force_str(opts.search_help_text or ""),
            "filters": service.filter_specs(opts, request),
            "ordering": opts.get_ordering(),
            "per_page": opts.list_per_page,
            "editable": opts.get_list_editable(),
            "bulk_edit_fields": opts.get_bulk_edit_fields(),
            "import_fields": opts.get_import_fields(),
            "date_hierarchy": (
                {"field": dh, "label": service.path_label(opts.model, dh)} if dh else None
            ),
            "show_full_result_count": opts.show_full_result_count,
            "actions": [
                {
                    "name": a.name,
                    "label": a.label,
                    "destructive": a.destructive,
                    "allowed": opts.has_perm(request, a.permission),
                }
                for a in opts.get_actions().values()
            ],
        },
        "form": {
            "fields": opts.get_fields(),
            "readonly_fields": opts.get_readonly_fields(),
            "password_fields": opts.password_fields,
            "fieldsets": opts.get_fieldsets(),
            "widgets": widgets,
            "prepopulated": dict(opts.prepopulated_fields),
            "save_as": opts.save_as,
            "save_as_continue": opts.save_as_continue,
            "save_on_top": opts.save_on_top,
            "change_password": opts.change_password_form is not None,
            "password_help": (
                users.password_help() if opts.password_fields or opts.add_form is not None else []
            ),
            "add": {
                "fields": add_fields,
                "fieldsets": opts.get_fieldsets(add=True),
                "custom_form": opts.add_form is not None,
            },
            "inlines": inlines.meta(opts, request),
        },
    }


@router.get("/recent-actions")
def recent_actions(request, limit: int = 10):
    """The current user's latest edits (the home page's "Recent actions")."""
    return {"entries": service.recent_actions(request, max(1, min(limit, 50)))}


@router.get("/graph")
def graph(request):
    """Relation graph between models the user can view."""
    visible = {o.label for o in service.visible_models(request)}
    g = get_registry().graph()
    return {
        "nodes": [n for n in g["nodes"] if n["label"] in visible],
        "edges": [e for e in g["edges"] if e["source"] in visible and e["target"] in visible],
    }


@router.get("/path")
def path(request, source: str, target: str):
    """Shortest relation path between two models (e.g. order → city)."""
    visible = {o.label for o in service.visible_models(request)}
    if source.lower() not in visible or target.lower() not in visible:
        raise service.CrudError(404, _("Unknown model"))
    steps = get_registry().find_path(source, target)
    if steps is None:
        return {"path": None, "lookup": None}
    return {
        "path": [s.__dict__ for s in steps],
        "lookup": "__".join(s.relation for s in steps),
    }
