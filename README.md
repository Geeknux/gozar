# Gozar — Django-Bi-Admin

A from-scratch replacement for `django.contrib.admin` with BI-grade reporting
and a natural-language (Persian-first) report interface. See
[DESIGN.md](DESIGN.md) for the full design.

**Status:** M1 (discovery + CRUD) done.

## Development

```sh
uv sync                                          # install deps + dev tools
uv run python example_project/manage.py migrate
uv run python example_project/manage.py seed_demo
uv run python example_project/manage.py createsuperuser
./scripts/build-frontend.sh                      # React SPA → src/bi_admin/static/bi_admin/spa
uv run python example_project/manage.py runserver  # open http://127.0.0.1:8000/gozar/
uv run pytest
```

Frontend hot reload: run `npm run dev` in `frontend/` and start Django with
`GOZAR_VITE_DEV_SERVER=http://localhost:5173`.

SQLite is used by default. For PostgreSQL (16+):

```sh
uv sync --extra postgres
GOZAR_DB=postgres POSTGRES_HOST=localhost uv run pytest
```

## Using it in a project

```python
# settings.py
INSTALLED_APPS += ["bi_admin"]

# urls.py
urlpatterns += [path("gozar/", include("bi_admin.urls"))]
```

Every model is discovered automatically. Customise one in a `gozar.py` module
inside any app (auto-discovered, like `admin.py`):

```python
from bi_admin.crud import ModelOptions, action, register

@register(Order)
class OrderOptions(ModelOptions):
    list_display = ["__str__", "customer", "customer__city__name", "status", "amount"]
    search_fields = ["customer__name", "^customer__phone"]
    list_filter = ["status", "created_at", "customer__city"]
    readonly_fields = ["paid_at"]
    actions = ["mark_cancelled"]

    @action("Cancel selected orders")
    def mark_cancelled(self, request, queryset):
        return {"updated": queryset.filter(status="pending").update(status="cancelled")}
```

Optional settings: `GOZAR = {"INCLUDE_APPS": [...], "EXCLUDE_APPS": [...],
"EXCLUDE_MODELS": ["app.model"], "VITE_DEV_SERVER": None}`.

The JSON API (django-ninja, staff session + CSRF) is documented at
`/gozar/api/docs`.

## Layout

- `src/bi_admin/` — the installable app:
  - `discovery/` — registry built from `_meta`, relation graph, lookup-path validation;
  - `crud/` — `ModelOptions` (the `ModelAdmin` counterpart) and the CRUD service;
  - `api/` — auth, metadata, and CRUD endpoints;
  - `semantic`, `engine`, `execution`, `access`, `calendar`, `nl` — later milestones.
- `frontend/` — React + TypeScript SPA (Vite), RTL, Jalali dates.
- `example_project/` — demo project with the `shop` app (City, Customer,
  Product, Order, OrderItem), its `gozar.py`, and the `seed_demo` command.
- `tests/` — pytest suite; `tests/nl_golden/` holds NL → ReportSpec fixtures.

## License

[MIT](LICENSE)
