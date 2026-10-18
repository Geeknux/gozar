from ninja import Router

from bi_admin.crud import service
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
    for f in info["fields"]:
        if f["name"] in opts.password_fields:
            f["kind"] = "password"
            f["required"] = False
    return {
        **info,
        "permissions": opts.permissions(request),
        "list": {
            "columns": [c.as_dict() for c in service.list_columns(opts)],
            "search_fields": opts.get_search_fields(),
            "filters": service.filter_specs(opts),
            "ordering": opts.get_ordering(),
            "per_page": opts.list_per_page,
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
        },
    }


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
        raise service.CrudError(404, "Unknown model")
    steps = get_registry().find_path(source, target)
    if steps is None:
        return {"path": None, "lookup": None}
    return {
        "path": [s.__dict__ for s in steps],
        "lookup": "__".join(s.relation for s in steps),
    }
