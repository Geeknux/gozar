# Gozar — Django-Bi-Admin

A from-scratch replacement for `django.contrib.admin` with BI-grade reporting
and a natural-language (Persian-first) report interface. See
[DESIGN.md](DESIGN.md) for the full design.

**Status:** M6 (dashboards and scheduling) done — save reports and share them
(each viewer sees their own rows), compare with the previous period or the same
period last year (in both calendars, also by asking "compared to last year" /
«نسبت به سال قبل»), build dashboards with drag-and-drop widgets and global
filters, pin one as your home page, and e-mail reports or dashboards daily,
weekly or monthly — every recipient gets their own scope, language and calendar.
On top of M5's natural-language interface (English and Persian, offline or with
any OpenAI-compatible model), M4's row-level rules, semantic visibility, audit
log, limits, cache, replica routing and background jobs, and M3's report
builder, charts, pivot, drill-down and export.

## Development

```sh
uv sync                                          # install deps + dev tools
uv run python example_project/manage.py migrate
uv run python example_project/manage.py seed_demo --users  # English data + demo accounts
uv run python example_project/manage.py gozar_calendar  # calendar table (Jalali reports)
./scripts/build-frontend.sh                      # React SPA → src/bi_admin/static/bi_admin/spa
uv run python example_project/manage.py runserver  # open http://127.0.0.1:8000/gozar/
uv run pytest
```

`seed_demo --users` creates the accounts below, all with the password
`gozar-demo` (`--password` changes it), to try the access rules:

| user      | sees                                                                 |
|-----------|----------------------------------------------------------------------|
| `admin`   | superuser: everything                                                |
| `manager` | all shop data, the finance-only *Refunds* measure, the report audit log and question log |
| `tehran`  | only Tehran, Karaj and Qom (row rules), no *Refunds*                 |
| `east`    | only Mashhad and Kerman                                               |

It also creates demo content owned by `manager` and shared with everyone: four
saved reports, the **Sales overview** dashboard (manager's home page) and a
weekly e-mail of it to `manager`, `tehran` and `east`. Send it now with
`manage.py gozar_send_schedules --schedule 1`: the e-mails land in
`example_project/.mail/` (file e-mail backend; set
`GOZAR_EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend` to print
them), and each recipient's numbers differ by region.

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

```jsonc
{
  "semantic_model": "order_metrics",
  "measures": ["revenue", "orders"],
  "dimensions": ["city"],
  "time_grain": "month",
  "calendar": "jalali",  // optional: defaults to the user's calendar (Gregorian)
  "relative_period": "last_3_months",
  "filters": [{"field": "status", "op": "in", "value": ["paid", "refunded"]}],
  "sort": [{"field": "revenue", "direction": "desc"}],
  "limit": 100
}
```

Grains are `day/week/month/season/year` in the Gregorian calendar (the default)
or the Jalali one (grouped in the database through the `CalendarDay` table — run
`manage.py gozar_calendar` once). Relative periods such as `this_month`, `last_season`,
`last_3_months`, `past_30_days` or `last_year` are resolved in Python against
today's date. `POST /gozar/api/reports/validate` returns structured errors with
suggestions, and `GET /gozar/api/reports/catalog` (user's language; `?lang=fa|en|all`) lists the semantic
models the user may report on. Reports need the same staff session and model
view permission as the CRUD API.

### Report UI

**Reports** in the sidebar opens the report builder: pick the data (semantic
model), measures, group-by fields, date field, grain, calendar, a relative
period or a custom date range, filters (value pickers search the dimension's
values), sort and row limit. The spec is validated as you edit — problems show
next to the field — and the report re-runs automatically. Results show as a
sortable table, bar/line/area/pie charts (Apache ECharts), a pivot table (two
group-by fields) or KPI cards (no group-by). Click any value, bar, slice or
pivot cell to open the records behind it, linked to their edit pages. Export
the result as CSV or Excel. The whole report lives in the URL
(`/gozar/reports?spec=…`), so **Copy link** shares it.

Endpoints behind it (all staff session + the model's view permission):
`GET /api/reports/values?semantic_model=&dimension=&q=` (filter values),
`POST /api/reports/drilldown` (`{"spec": …, "cell": {"city": "Tehran",
"created_at": "2026-09-01"}, "measure": "revenue", "page": 1}` → paginated
records) and `POST /api/reports/export?format=csv|xlsx` (body: the spec;
headers in the user's language).

### Comparisons, saved reports, dashboards, e-mail

- **Comparison periods**: `ReportSpec.compare_to` = `previous_period` (the span
  just before: `last_3_months` → the 3 months before, `this_month` to date → the
  same days of last month) or `same_period_last_year` (a year back in the
  report's calendar; weeks move 52 weeks). It needs a period — a relative period
  or a date range on the time dimension. Each measure gets
  `<measure>__compare`, `__change` and `__change_pct` columns (role
  `comparison`), and time buckets line up across the two periods (Esfand 1404 ↔
  Esfand 1403, Dec 2025 ↔ Dec 2024). The builder's **Compare to** control shows
  them as table columns with ▲/▼, KPI deltas and a dashed second series in line
  and bar charts; clicking a comparison value drills into the comparison period
  (`"comparison": true` in `/reports/drilldown`).
- **Saved reports** (`/api/saved-reports`, **Saved reports** in the sidebar):
  named specs with a description and sharing — only me, specific users and
  groups, or everyone who may see the data. Save, save as, rename/share,
  duplicate and delete from the builder (`/reports?saved=<id>`) and from an Ask
  answer. Only the owner edits; anyone who can open one may duplicate it. A
  shared report always runs as the viewer: their permissions, visible measures
  and row-level scope.
- **Dashboards** (`/api/dashboards`, **Dashboards**): a 12-column grid of
  widgets — saved reports or reports defined in the dashboard — each with its own
  chart and size. The owner adds, removes, resizes and drags them into place
  (react-grid-layout) and picks which category filters viewers get; viewers set
  a period or date range and values (e.g. cities), which apply to every widget
  whose data has that field (`POST /api/dashboards/<id>/resolve` returns each
  widget's effective spec). Widgets load in parallel through `/reports/run`
  (result cache and background jobs included) and keep drill-down and export.
  **Pin as home page** shows a dashboard on the home page.
- **E-mail schedules** (`/api/schedules`, **Email schedules**): a saved report or
  dashboard sent daily, weekly or monthly (a Gregorian or Jalali day of the
  month) at a local time in a time zone (the owner's **Settings → Time zone** by
  default). Recipients are users of the system who can open it; each gets an HTML
  e-mail with the tables and KPI values (comparisons included) built as them — in
  their language and calendar — plus CSV or XLSX attachments. Send due schedules
  with `manage.py gozar_send_schedules` from cron (`--dry-run`, `--schedule ID`),
  or let `gozar_worker` do it (`WORKER_SENDS_SCHEDULES`). Uses Django's e-mail
  framework (`EMAIL_BACKEND`, `DEFAULT_FROM_EMAIL` or `GOZAR["EMAIL_FROM"]`);
  `GOZAR["BASE_URL"]` adds an "Open in Gozar" link. Every e-mail is logged
  (`GET /api/audit/deliveries`, **Audit log → Email deliveries**) and every report
  it ran is in the audit log with kind *Scheduled email*.

### Language and calendar

English and Gregorian are the defaults; Persian (RTL, Persian digits) and the
Jalali calendar are a per-user choice on the **Settings** page (top bar), saved
through `GET/PATCH /gozar/api/preferences`. API labels, verbose names,
validation errors and report labels come back in the user's language, and a
`ReportSpec` without `calendar` uses the user's calendar. Add the middleware
(it replaces Django's `LocaleMiddleware`) and list both languages:

```python
MIDDLEWARE = [
    ...,
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "bi_admin.middleware.PreferencesMiddleware",
    ...,
]
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("fa", "فارسی")]
GOZAR = {"DEFAULT_LANGUAGE": "en", "DEFAULT_CALENDAR": "gregorian"}
```

Translations ship as gettext catalogs (`src/bi_admin/locale/fa`, compiled `.mo`
included). After changing strings, run `django-admin makemessages -l fa` and
`django-admin compilemessages` in `src/bi_admin` (and `example_project/shop`);
SPA strings live in `frontend/src/i18n/en.ts` and `fa.ts`.

### Access control

**Row-level rules** decide which rows a user may see and touch. A rule takes the
user and returns a `Q`, `True`/`None` (no restriction) or `False` (no rows):

```python
from bi_admin.access import rule

rule(Order, lambda user: Q(customer__city__in=user.profile.cities.all()))  # a model
rule(OrderMetrics, lambda user: Q(status="paid"))  # reports on one semantic model only

@register(Customer)
class CustomerOptions(ModelOptions):
    row_rules = [lambda user: Q(city__in=user.profile.cities.all())]  # same, on the options
```

Rules are AND-ed and applied in the engine and the CRUD service, so every path
honours them: report run, drill-down, export, filter value pickers, background
jobs, CRUD list/filters/detail/history, autocomplete and relation fields,
actions and delete plans. Related rows outside the scope never show either: M2M
lists leave them out (saving keeps those hidden links), a hidden FK target and
list cells read through one show as *Restricted*. Saving a row so it would fall outside the user's scope
is refused (`403 out_of_scope`), and a delete that would cascade into rows they
can't see is blocked. Superusers bypass rules (`ROW_RULES_BYPASS_SUPERUSER`). A
rule that raises denies every row. The demo (`shop/gozar.py`) limits the
"Sales: … region" groups to their cities.

**Semantic visibility**: `Dimension(..., permission="shop.view_margin")` or
`Measure(..., permission=lambda user: ...)` (a permission, a list of them, or a
predicate). What a user may not see is left out of their catalog and rejected
exactly like an unknown key — in validation, sort, filters, drill-down, export
and value lookups — and list columns only such items expose don't appear in
drill-down rows. The demo's *Refunds* measure needs `shop.change_order`.

**Audit log**: every run, export and drill-down (also refused and failed ones,
and background jobs) is stored in `bi_admin.ReportLog` with the user, the
normalised spec, the row-level scope (the WHERE clause), duration, row count,
status, error code and cache hit. Users with `bi_admin.view_reportlog` get
**Audit log** in the sidebar (`GET /api/audit/reports`, filterable).

### Execution: limits, cache, replica, background jobs

- **Limits**: `REPORT_MAX_ROWS`, `REPORT_MAX_DIMENSIONS`, `REPORT_MAX_FILTERS`
  (`422` with `limit_exceeded`, `too_many_dimensions`, `too_many_filters`) and
  `REPORT_STATEMENT_TIMEOUT_MS` (PostgreSQL `SET LOCAL statement_timeout`; an
  interrupting progress handler on SQLite) → `504 statement_timeout`.
- **Result cache** (Django's cache framework, `REPORT_CACHE` alias,
  `REPORT_CACHE_TIMEOUT` seconds, `0` = off): keyed by the normalised spec, the
  user's scope, the language and a data version per model the report reads,
  which changes on every save/delete (signals) — call
  `bi_admin.execution.cache.bump(Model)` after bulk writes. The response's
  `cache` says `{"hit", "stored_at"}`; `?cache=false` (the SPA's **Refresh**)
  bypasses it. Use a cache shared by all processes (Redis, Memcached, files —
  not `LocMemCache`) when you run several workers or the job worker.
- **Read replica**: `GOZAR["REPORTS_DATABASE"] = "replica"` plus
  `DATABASE_ROUTERS = ["bi_admin.execution.router.ReportsRouter"]` sends report,
  drill-down, export and value-lookup reads there; CRUD, all writes, the audit
  log and jobs stay on `default`.
- **Background jobs**: reports run synchronously by default. With
  `REPORT_ASYNC = True`, `Semantic.run_async = True` or `POST
  /api/reports/run?mode=async`, the API answers `202 {"job": …}` and the SPA
  polls `GET /api/reports/jobs/<id>` (then `/result`, `/export`, `/cancel`).
  The default `DatabaseBackend` stores jobs in `bi_admin.ReportJob`; run
  `manage.py gozar_worker` (`--burst` to drain the queue and exit). Running jobs
  hold a lease kept alive by a heartbeat; if a worker dies, its job is requeued
  after `JOB_LEASE_SECONDS` (60) and failed (`worker_lost`) after
  `JOB_MAX_ATTEMPTS` (3). Plug in
  another `JOB_BACKEND` (e.g. one that enqueues a Celery task calling
  `bi_admin.execution.jobs.execute(job_id)`).

### Asking questions (natural language)

The **Ask** box on the home page and at the top of **Reports** takes a question
in English or Persian. Gozar turns it into a `ReportSpec` — never SQL — checks
it with the same validator, permissions and row rules as the builder, and shows
an interpretation sentence ("Revenue by City, the last 3 months (Jul 1 – Sep 30,
2026), where Status is Paid.") with the report. Follow-up questions edit the
current report ("now only Tehran", "make it monthly", "by province instead",
«فقط تهران», «ماهانه‌اش کن»); **Open in report builder** hands the spec to the
normal filter UI.

- **Offline by default.** With no model configured, a deterministic interpreter
  answers: measures, group-by fields, time grain, relative periods, explicit
  dates/years/months (Jalali or Gregorian), category and choice filters, number
  comparisons, top/bottom N, sort and chart type, in both languages. It uses the
  semantic layer's labels and synonyms, so better synonyms mean better answers.
- **Values are matched on the server**: «تهران» finds `Tehran` (and vice versa)
  through letter normalisation, transliteration and fuzzy matching against the
  distinct values the *user* may see; an unknown value gets a "did you mean"
  question with in-scope suggestions only.
- **Privacy**: only catalog metadata (keys, labels, synonyms, descriptions,
  declared choices) and the user's words reach a model — never rows. Category
  values in a spec being refined are sent as placeholders (`@v1`).
- **Relative dates are resolved by Gozar**, not by the model, in the user's
  calendar.

**Pointing Gozar at a real LLM** (any OpenAI-compatible `/chat/completions`):

```sh
# hosted (OpenAI or a compatible gateway)
export GOZAR_NL_BASE_URL=https://api.openai.com/v1
export GOZAR_NL_MODEL=gpt-4o-mini
export GOZAR_NL_API_KEY=sk-...            # read from the environment only

# or local: Ollama / vLLM (no key needed)
export GOZAR_NL_BASE_URL=http://localhost:11434/v1   # vLLM: http://localhost:8000/v1
export GOZAR_NL_MODEL=qwen2.5:7b-instruct
```

or in settings (the key still comes from the environment; a literal key in
settings fails system check `bi_admin.E003`):

```python
GOZAR = {
    "NL": {
        "BASE_URL": "http://localhost:11434/v1",
        "MODEL": "qwen2.5:7b-instruct",
        "API_KEY_ENV": "GOZAR_NL_API_KEY",  # name of the env variable holding the key
        "TIMEOUT": 30,
        "RESPONSE_FORMAT": "json_schema",   # or "json_object" / "none" for older servers
    }
}
```

The model must return the spec as JSON (structured outputs are requested and
JSON mode is the automatic fallback). Invalid specs are sent back with the
validation errors (`MAX_RETRIES`, 2); if the server can't be reached, the
offline interpreter answers instead (`FALLBACK`). Other `GOZAR["NL"]` keys (see
`bi_admin/nl/conf.py`): `PROVIDER` (`auto` | `openai` | `heuristic` | `stub` |
a dotted path), `TEMPERATURE`, `EXTRA_HEADERS`, `TOP_K` and `MAX_MODELS`
(catalog retrieval), `VALUE_INDEX_MAX`/`VALUE_INDEX_TIMEOUT`, `CACHE_TIMEOUT`
(NL → spec cache, 3600 s), `LOG_PROMPTS` (opt-in: keep prompts and raw outputs
for evaluation), `MAX_QUESTION_LENGTH` (500) and `RATE_LIMIT` (30 questions per
user per minute).

Every question is recorded in `bi_admin.NLLog` (readable with
`bi_admin.view_nllog` on the **Question log** page or `GET
/api/audit/questions`); reports it runs are in the report audit log as usual.
Declare few-shot examples per semantic model with `nl_examples = [{"question":
…, "spec": {…}}]` — they also become the suggested questions in the UI.

API: `POST /api/nl/ask {"question", "previous"?, "execute"?}` → `{status: ok |
clarification | invalid, spec, interpretation, clarification, suggestions,
warnings, …}` (with `execute`, also the report `result` or a background
`job`); `GET /api/nl/status`; `POST /api/nl/interpret {"spec"}`.
`POST /api/reports/validate` returns the `interpretation` too.

**Evaluation**: `tests/nl_golden/` holds 179 questions (122 Persian, 37 English,
20 follow-ups) with their expected specs. CI scores them against the offline
interpreter and a stub model; score a real model with

```sh
GOZAR_NL_BASE_URL=... GOZAR_NL_MODEL=... uv run python example_project/manage.py \
    gozar_nl_eval --verbose-failures --json nl-report.json
```

(exact and semantic match rate, clarification rate and recall, retry rate;
`--provider`, `--only fa`, `--user`, `--fail-under 0.9`). Expected values are
those of the English demo data (`seed_demo`).

Optional settings (defaults in `bi_admin/conf.py`): `GOZAR = {"INCLUDE_APPS": [...],
"EXCLUDE_APPS": [...], "EXCLUDE_MODELS": ["app.model"], "VITE_DEV_SERVER": None,
"WEEKEND_DAYS": [4], "DEFAULT_LANGUAGE": "en", "DEFAULT_CALENDAR": "gregorian",
"ROW_RULES_BYPASS_SUPERUSER": True, "AUDIT_REPORTS": True, "REPORT_MAX_ROWS": 10000,
"REPORT_MAX_DIMENSIONS": 6, "REPORT_MAX_FILTERS": 20, "REPORT_STATEMENT_TIMEOUT_MS": 30000,
"REPORTS_DATABASE": None, "REPORT_CACHE": "default", "REPORT_CACHE_TIMEOUT": 300,
"REPORT_ASYNC": False, "JOB_BACKEND": "bi_admin.execution.jobs.DatabaseBackend",
"JOB_LEASE_SECONDS": 60, "JOB_MAX_ATTEMPTS": 3, "API_DOCS": "staff", "NL": {…},
"EMAIL_FROM": None, "EMAIL_MAX_ROWS": 50, "BASE_URL": None, "WORKER_SENDS_SCHEDULES": True}`.

The JSON API (django-ninja, staff session + CSRF) is documented at
`/gozar/api/docs` (Swagger UI; schema at `/gozar/api/openapi.json`). The docs are
for staff only by default — anonymous visitors are sent to the login first; set
`GOZAR["API_DOCS"] = "public"` to open them. They work wherever you mount
`bi_admin.urls`, including under your own instance namespace
(`include("bi_admin.urls", namespace="reports")`).

## Layout

- `src/bi_admin/` — the installable app:
  - `discovery/` — registry built from `_meta`, relation graph, lookup-path validation;
  - `crud/` — `ModelOptions` (the `ModelAdmin` counterpart) and the CRUD service;
  - `semantic/` — `Semantic`, `Dimension`, `Measure`, `@register`, the catalog;
  - `engine/` — `ReportSpec`, validator, ORM compiler, runner, drill-down,
    export (CSV/XLSX), dimension values;
  - `calendar/` — Jalali helpers, `CalendarDay` table builder, relative periods;
  - `access/` — row-level rules and the report audit log;
  - `execution/` — statement timeout, result cache, replica router, background jobs;
  - `api/` — auth, metadata, CRUD, report, job, audit, preferences, saved-report,
    dashboard and schedule endpoints;
  - `sharing.py`, `dashboards.py`, `scheduling.py`, `formatting.py` — who may
    open saved reports and dashboards, widget specs and global filters, e-mail
    schedules (due times, per-recipient e-mails), server-side value formatting;
  - `preferences.py`, `middleware.py` — per-user language/calendar;
  - `locale/fa/` — Persian gettext catalog;
  - `nl/` — the NL interface: text normalisation and transliteration, the
    visible catalog and retrieval, the value index, the prompt, providers
    (OpenAI-compatible, offline heuristic, stub), the pipeline, the
    interpretation sentence and the golden-set evaluation;
- `frontend/` — React + TypeScript SPA (Vite); `src/i18n/` holds the en/fa
  dictionaries and formatting (LTR/RTL, digits, Gregorian/Jalali dates),
  `src/reports/` the report builder, results, ECharts charts, drill-down and
  the Ask box (`AskBox.tsx`, `AskPanel.tsx`) and saved reports,
  `src/dashboards/` the dashboard view, grid editor and global filters,
  `src/schedules/` e-mail schedules,
  `src/components/DatePicker.tsx` the Gregorian/Jalali date picker.
- `example_project/` — demo project with the `shop` app (City, Customer,
  Product, Order, OrderItem), its `gozar.py` (CRUD options + `OrderMetrics`),
  and the `seed_demo` command (`--seed`, `--customers`, `--orders`, `--days`,
  `--flush`, `--locale en|fa`, `--users`, `--password`). The default English data set uses real Iranian
  cities with English names; `--locale fa` writes the same data with Persian
  names, handy for RTL demos and Arabic/Persian letter search
  (`seed_demo --flush --locale fa`).
- `tests/` — pytest suite; `tests/nl_golden/` holds the NL golden set
  (question → expected ReportSpec, JSON).

## License

[MIT](LICENSE)
