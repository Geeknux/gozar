# Gozar — Django-Bi-Admin

A from-scratch replacement for `django.contrib.admin` with BI-grade reporting
and a natural-language (Persian-first) report interface. See
[DESIGN.md](DESIGN.md) for the full design.

**Status:** M2 (semantic layer + query engine) done.

## Development

```sh
uv sync                                          # install deps + dev tools
uv run python example_project/manage.py migrate
uv run python example_project/manage.py seed_demo
uv run python example_project/manage.py gozar_calendar     # Jalali calendar table
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

### Reports: semantic models

Describe what can be reported on in the same `gozar.py`. Labels, synonyms and
descriptions (Persian and English) feed the catalog and the NL interface:

```python
from django.db.models import Count, Q, Sum
from bi_admin.semantic import Dimension, Measure, Semantic, register

@register
class OrderMetrics(Semantic):          # ReportSpec.semantic_model = "order_metrics"
    model = Order
    time_dimension = "created_at"
    dimensions = {
        "created_at": Dimension("created_at", type="time",
                                label={"fa": "تاریخ ثبت سفارش", "en": "Order date"}),
        "city": Dimension("customer__city__name", label={"fa": "شهر", "en": "City"},
                          synonyms={"fa": ["محل"]}),
        "status": Dimension("status"),  # type and labels inferred from the field
    }
    measures = {
        "revenue": Measure(Sum("amount", filter=Q(status="paid")), format="currency",
                           label={"fa": "درآمد", "en": "Revenue"},
                           synonyms={"fa": ["فروش", "گردش مالی"], "en": ["sales"]}),
        "orders": Measure(Count("id"), label={"fa": "تعداد سفارش", "en": "Orders"}),
    }
```

Then run a `ReportSpec` (`POST /gozar/api/reports/run`, or
`bi_admin.engine.run(spec, request)` in Python):

```json
{
  "semantic_model": "order_metrics",
  "measures": ["revenue", "orders"],
  "dimensions": ["city"],
  "time_grain": "month",
  "calendar": "jalali",
  "relative_period": "last_3_months",
  "filters": [{"field": "status", "op": "in", "value": ["paid", "refunded"]}],
  "sort": [{"field": "revenue", "direction": "desc"}],
  "limit": 100
}
```

Grains are `day/week/month/season/year` in the Jalali (grouped in the database
through the `CalendarDay` table — run `manage.py gozar_calendar` once) or
Gregorian calendar. Relative periods such as `this_month`, `last_season`,
`last_3_months`, `past_30_days` or `last_year` are resolved in Python against
today's date. `POST /gozar/api/reports/validate` returns structured errors with
suggestions, and `GET /gozar/api/reports/catalog?lang=fa` lists the semantic
models the user may report on. Reports need the same staff session and model
view permission as the CRUD API.

Optional settings: `GOZAR = {"INCLUDE_APPS": [...], "EXCLUDE_APPS": [...],
"EXCLUDE_MODELS": ["app.model"], "VITE_DEV_SERVER": None, "REPORT_MAX_ROWS": 10000,
"REPORT_MAX_DIMENSIONS": 6, "WEEKEND_DAYS": [4]}`.

The JSON API (django-ninja, staff session + CSRF) is documented at
`/gozar/api/docs`.

## Layout

- `src/bi_admin/` — the installable app:
  - `discovery/` — registry built from `_meta`, relation graph, lookup-path validation;
  - `crud/` — `ModelOptions` (the `ModelAdmin` counterpart) and the CRUD service;
  - `semantic/` — `Semantic`, `Dimension`, `Measure`, `@register`, the catalog;
  - `engine/` — `ReportSpec`, validator, ORM compiler, runner;
  - `calendar/` — Jalali helpers, `CalendarDay` table builder, relative periods;
  - `api/` — auth, metadata, CRUD and report endpoints;
  - `execution`, `access`, `nl` — later milestones.
- `frontend/` — React + TypeScript SPA (Vite), RTL, Jalali dates.
- `example_project/` — demo project with the `shop` app (City, Customer,
  Product, Order, OrderItem), its `gozar.py` (CRUD options + `OrderMetrics`),
  and the `seed_demo` command.
- `tests/` — pytest suite; `tests/nl_golden/` holds NL → ReportSpec fixtures.

## License

[MIT](LICENSE)
