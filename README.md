# Gozar — Django-Bi-Admin

A from-scratch replacement for `django.contrib.admin` with BI-grade reporting
and a natural-language (Persian-first) report interface. See
[DESIGN.md](DESIGN.md) for the full design.

**Status:** M0 (skeleton).

## Development

```sh
uv sync                                          # install deps + dev tools
uv run python example_project/manage.py migrate
uv run python example_project/manage.py seed_demo
uv run python example_project/manage.py runserver  # GET /gozar/api/health
uv run pytest
```

SQLite is used by default. For PostgreSQL (16+):

```sh
uv sync --extra postgres
GOZAR_DB=postgres POSTGRES_HOST=localhost uv run pytest
```

## Layout

- `src/bi_admin/` — the installable app (`discovery`, `semantic`, `engine`,
  `execution`, `access`, `calendar`, `nl`, `api`).
- `example_project/` — demo project with the `shop` app (City, Customer,
  Product, Order, OrderItem) and the `seed_demo` command.
- `tests/` — pytest suite; `tests/nl_golden/` holds NL → ReportSpec fixtures.
