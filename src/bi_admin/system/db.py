"""Database statistics for the System page, per backend.

``database_stats(alias)`` lists every table with its model, estimated rows and
sizes, plus the indexes per table and the database size:

* PostgreSQL: ``pg_class``/``pg_namespace`` (ordinary and partitioned tables of
  the schemas on the search path), ``pg_table_size``, ``pg_indexes_size``,
  ``pg_total_relation_size``, ``pg_database_size``; estimated rows from
  ``reltuples`` (``-1`` = never analysed → ``pg_stat_user_tables.n_live_tup``),
  plus scan and vacuum/analyze statistics; index sizes by ``pg_relation_size``.
* SQLite: the ``dbstat`` virtual table when compiled in (index pages attributed
  to their table), else ``page_count × page_size`` for the total only; estimated
  rows from ``sqlite_stat1`` when ``ANALYZE`` has run.
* Anything else: the introspected table list without sizes.

Only names returned by introspection are ever put into SQL (quoted), so the
exact-count endpoint can't be used to run anything else.
"""

from __future__ import annotations

import time
from typing import Any

from django.apps import apps
from django.conf import settings
from django.db import DatabaseError, connections
from django.utils.translation import gettext as _

from bi_admin.conf import setting

DJANGO_TABLES = {"django_migrations"}  # Django bookkeeping without a model
PREAGG_PREFIX = "gozar_pa_"  # bi_admin.scale.preaggregations.TABLE_PREFIX


def database_alias() -> str:
    """The reports database when configured (statistics are read there), else default."""
    alias = setting("REPORTS_DATABASE")
    return alias if alias and alias in settings.DATABASES else "default"


def server_info(alias: str) -> dict[str, Any]:
    """Engine, version, server and database name — never user names, passwords or
    connection OPTIONS."""
    connection = connections[alias]
    conf = settings.DATABASES[alias]
    name = str(conf.get("NAME") or "")
    if connection.vendor == "sqlite":
        name = name.replace("\\", "/").rsplit("/", 1)[-1]  # the file, not its path
    return {
        "alias": alias,
        "vendor": connection.vendor,
        "engine": conf.get("ENGINE", ""),
        "display_name": connection.display_name,
        "version": _server_version(connection),
        "name": name,
        "host": conf.get("HOST") or None,
        "port": str(conf["PORT"]) if conf.get("PORT") else None,
    }


def _server_version(connection) -> str | None:
    try:
        if connection.vendor == "sqlite":
            import sqlite3

            return sqlite3.sqlite_version
        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute("SHOW server_version")
            elif connection.vendor in ("mysql", "oracle"):
                cursor.execute(
                    "SELECT VERSION()"
                    if connection.vendor == "mysql"
                    else "SELECT banner FROM v$version WHERE ROWNUM = 1"
                )
            else:
                return None
            return str(cursor.fetchone()[0])
    except DatabaseError:
        return None


# --- table → model mapping -----------------------------------------------------------


def table_models() -> dict[str, dict[str, Any]]:
    """``db_table`` → model, app and kind for every installed model (auto-created
    many-to-many tables included)."""
    out: dict[str, dict[str, Any]] = {}
    for model in apps.get_models(include_auto_created=True):
        opts = model._meta
        if opts.proxy:
            continue
        django_app = opts.app_config.name.startswith("django.")
        if django_app:
            kind = "django"
        elif opts.auto_created:
            kind = "m2m"
        elif not opts.managed:
            kind = "unmanaged"
        else:
            kind = "model"
        out.setdefault(
            opts.db_table,
            {
                "model": opts.label_lower,
                "app": opts.app_label,
                "app_verbose_name": str(opts.app_config.verbose_name),
                "verbose_name": str(opts.verbose_name),
                "kind": kind,
            },
        )
    return out


def classify(table: str, known: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if table in known:
        return dict(known[table])
    kind = "django" if table in DJANGO_TABLES else "other"
    if table.startswith(PREAGG_PREFIX):
        kind = "preaggregation"  # a summary table Gozar maintains (M7)
    return {
        "model": None,
        "app": None,
        "app_verbose_name": None,
        "verbose_name": None,
        "kind": kind,
    }


def table_names(alias: str) -> list[str]:
    connection = connections[alias]
    with connection.cursor() as cursor:
        return sorted(
            t.name for t in connection.introspection.get_table_list(cursor) if t.type == "t"
        )


# --- per-backend statistics ----------------------------------------------------------


def _empty_table(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "estimated_rows": None,
        "table_size": None,
        "index_size": None,
        "total_size": None,
    }


def postgres_stats(alias: str) -> dict[str, Any]:
    connection = connections[alias]
    tables: dict[str, dict[str, Any]] = {}
    indexes: dict[str, list[dict[str, Any]]] = {}
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_database_size(current_database())")
        total = int(cursor.fetchone()[0])
        cursor.execute(
            """
            SELECT c.relname, n.nspname, c.reltuples, pg_table_size(c.oid),
                   pg_indexes_size(c.oid), pg_total_relation_size(c.oid),
                   s.n_live_tup, s.seq_scan, s.idx_scan,
                   GREATEST(s.last_vacuum, s.last_autovacuum),
                   GREATEST(s.last_analyze, s.last_autoanalyze)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
            WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY (current_schemas(false))
            """
        )
        for row in cursor.fetchall():
            name, schema, reltuples, tsize, isize, total_size, live, seq, idx, vac, ana = row
            estimate = int(reltuples) if reltuples is not None and reltuples >= 0 else live
            tables[name] = {
                "name": name,
                "schema": schema,
                "estimated_rows": int(estimate) if estimate is not None else None,
                "table_size": int(tsize),
                "index_size": int(isize),
                "total_size": int(total_size),
                "seq_scans": seq,
                "index_scans": idx,
                "last_vacuum": vac.isoformat() if vac else None,
                "last_analyze": ana.isoformat() if ana else None,
            }
        cursor.execute(
            """
            SELECT t.relname, i.relname, ix.indisunique, ix.indisprimary,
                   pg_relation_size(i.oid),
                   ARRAY(SELECT a.attname FROM unnest(ix.indkey) WITH ORDINALITY k(attnum, ord)
                         JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                         ORDER BY k.ord)
            FROM pg_index ix
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_class t ON t.oid = ix.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE t.relkind IN ('r', 'p') AND n.nspname = ANY (current_schemas(false))
            ORDER BY t.relname, i.relname
            """
        )
        for table, name, unique, primary, size, columns in cursor.fetchall():
            indexes.setdefault(table, []).append(
                {
                    "name": name,
                    "columns": list(columns or []),
                    "unique": bool(unique),
                    "primary": bool(primary),
                    "size": int(size),
                }
            )
    return {"method": "postgresql", "total_size": total, "tables": tables, "indexes": indexes}


def _sqlite_indexes(cursor, table: str, sizes: dict[str, int] | None) -> list[dict[str, Any]]:
    cursor.execute(f"PRAGMA index_list({quote_sqlite(table)})")
    out = []
    for row in cursor.fetchall():
        name, unique, origin = row[1], row[2], row[3]
        cursor.execute(f"PRAGMA index_info({quote_sqlite(name)})")
        columns = [r[2] for r in cursor.fetchall() if r[2] is not None]
        out.append(
            {
                "name": name,
                "columns": columns,
                "unique": bool(unique),
                "primary": origin == "pk",
                "size": sizes.get(name) if sizes is not None else None,
            }
        )
    return sorted(out, key=lambda i: i["name"])


def quote_sqlite(name: str) -> str:
    """Quote an introspected SQLite name for a PRAGMA argument."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def sqlite_dbstat(cursor) -> dict[str, int]:
    """Bytes per table/index from the ``dbstat`` virtual table (raises when it isn't
    compiled into this SQLite)."""
    cursor.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")
    return {name: int(size) for name, size in cursor.fetchall()}


def sqlite_stats(alias: str) -> dict[str, Any]:
    connection = connections[alias]
    names = table_names(alias)
    notes: list[str] = []
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA page_count")
        pages = int(cursor.fetchone()[0])
        cursor.execute("PRAGMA page_size")
        total = pages * int(cursor.fetchone()[0])
        try:
            sizes: dict[str, int] | None = sqlite_dbstat(cursor)
            method = "dbstat"
        except DatabaseError:
            sizes, method = None, "page_count"
            notes.append(
                _(
                    "This SQLite build has no dbstat table: only the total database size "
                    "is known, not the size of each table."
                )
            )
        cursor.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'")
        index_tables = dict(cursor.fetchall())
        estimates: dict[str, int] = {}
        cursor.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_stat1'")
        if cursor.fetchone():
            cursor.execute("SELECT tbl, stat FROM sqlite_stat1")
            for tbl, stat in cursor.fetchall():
                try:
                    estimates[tbl] = max(estimates.get(tbl, 0), int(str(stat).split()[0]))
                except (ValueError, IndexError):
                    continue
        tables: dict[str, dict[str, Any]] = {}
        indexes: dict[str, list[dict[str, Any]]] = {}
        for name in names:
            row = _empty_table(name)
            row["estimated_rows"] = estimates.get(name)
            if sizes is not None:
                own = sizes.get(name, 0)
                idx = sum(s for n, s in sizes.items() if index_tables.get(n) == name)
                row.update(table_size=own, index_size=idx, total_size=own + idx)
            tables[name] = row
            indexes[name] = _sqlite_indexes(cursor, name, sizes)
    return {
        "method": method,
        "total_size": total,
        "tables": tables,
        "indexes": indexes,
        "notes": notes,
    }


def generic_stats(alias: str) -> dict[str, Any]:
    connection = connections[alias]
    tables = {name: _empty_table(name) for name in table_names(alias)}
    indexes: dict[str, list[dict[str, Any]]] = {}
    with connection.cursor() as cursor:
        for name in tables:
            try:
                constraints = connection.introspection.get_constraints(cursor, name)
            except (DatabaseError, NotImplementedError):
                continue
            indexes[name] = sorted(
                (
                    {
                        "name": key,
                        "columns": list(c.get("columns") or []),
                        "unique": bool(c.get("unique")),
                        "primary": bool(c.get("primary_key")),
                        "size": None,
                    }
                    for key, c in constraints.items()
                    if c.get("index") or c.get("primary_key") or c.get("unique")
                ),
                key=lambda i: i["name"],
            )
    return {
        "method": None,
        "total_size": None,
        "tables": tables,
        "indexes": indexes,
        "notes": [
            _("Sizes and row estimates are not available for the %(backend)s backend.")
            % {"backend": connection.display_name}
        ],
    }


BACKENDS = {"postgresql": postgres_stats, "sqlite": sqlite_stats}


def database_stats(alias: str | None = None) -> dict[str, Any]:
    alias = alias or database_alias()
    vendor = connections[alias].vendor
    collect = BACKENDS.get(vendor, generic_stats)
    started = time.monotonic()
    raw = collect(alias)
    known = table_models()
    tables = []
    for name, row in sorted(raw["tables"].items()):
        tables.append(
            {
                **row,
                **classify(name, known),
                "indexes": raw["indexes"].get(name, []),
            }
        )
    return {
        **server_info(alias),
        "size_method": raw["method"],
        "total_size": raw["total_size"],
        "table_count": len(tables),
        "tables": tables,
        "notes": raw.get("notes", []),
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


class UnknownTable(LookupError):
    pass


def exact_count(table: str, alias: str | None = None) -> dict[str, Any]:
    """``COUNT(*)`` of one introspected table, under the report statement timeout."""
    import bi_admin.engine  # noqa: F401  (limits ↔ engine import cycle: engine first)
    from bi_admin.execution.limits import statement_timeout

    alias = alias or database_alias()
    if table not in set(table_names(alias)):
        raise UnknownTable(table)
    connection = connections[alias]
    started = time.monotonic()
    with statement_timeout(alias):
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {connection.ops.quote_name(table)}")
            rows = int(cursor.fetchone()[0])
    return {
        "table": table,
        "alias": alias,
        "rows": rows,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
