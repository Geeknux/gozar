"""Building and refreshing pre-aggregation tables.

* **Rebuild** — a new table (next generation) is created with Django's schema
  editor, filled with one ``INSERT … SELECT`` of the grouped query, and only then
  made current; the previous table is dropped.
* **Incremental** — in one transaction, the buckets of a time window are deleted
  and recomputed: the last ``incremental_window`` days plus every day that writes
  touched since the last refresh (``Materialization.dirty_from/dirty_to``) and the
  null bucket. When writes couldn't be narrowed down to days (``needs_rebuild``,
  or the copy is stale without records), a rebuild runs instead.

Both capture the data versions of every model the build reads *before* reading
them; the copy is fresh until one of them changes (``bi_admin.scale.state``).
A build lease on the state row keeps two builders of the same pre-aggregation
from running at once.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
import uuid
from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Q
from django.utils import timezone

from bi_admin.calendar import periods, table
from bi_admin.conf import setting
from bi_admin.engine.errors import ReportError
from bi_admin.execution import cache
from bi_admin.models import Materialization
from bi_admin.scale import state as states
from bi_admin.scale.preaggregations import TABLE_PREFIX, TIME_COLUMN, PreAggregation

logger = logging.getLogger("bi_admin.scale")
Kind = Materialization.Kind


class BuildBusy(Exception):
    """Another process is building this pre-aggregation."""


def database() -> str:
    return setting("PREAGG_DATABASE") or DEFAULT_DB_ALIAS


def source(preagg: PreAggregation):
    """The rows a pre-aggregation summarises: the semantic model's base queryset as
    trusted code sees it (no row rules — those are applied when it is read)."""
    return preagg.semantic.get_queryset(None)


def insert_select(preagg: PreAggregation, model, qs, using: str) -> int:
    """``INSERT INTO <table> (…) SELECT …`` from the grouped query; returns rows."""
    compiler = qs.query.get_compiler(using=using)
    sql, params = compiler.as_sql()
    names = [alias for _expr, _sql, alias in compiler.select]
    known = {c.name for c in preagg.columns}
    if set(names) != known:
        raise RuntimeError(f"Unexpected columns {names} for {preagg.key}")
    quote = connections[using].ops.quote_name
    columns = ", ".join(quote(model._meta.get_field(n).column) for n in names)
    with connections[using].cursor() as cursor:
        cursor.execute(f"INSERT INTO {quote(model._meta.db_table)} ({columns}) {sql}", params)
        return max(cursor.rowcount, 0)


def dependencies(preagg: PreAggregation, using: str) -> list[str]:
    try:
        sql = str(preagg.build_queryset(source(preagg)).query)
    except Exception:  # EmptyResultSet & co
        sql = ""
    return cache.dependencies(sql, using, [preagg.semantic.model_label])


def check_calendar(preagg: PreAggregation, using: str, window=None) -> None:
    """Jalali buckets coarser than a day come from ``CalendarDay``: refuse to store
    null buckets when it doesn't cover the source's dates."""
    from django.db.models import Max, Min

    from bi_admin.engine.compiler import date_range_q, local_date

    grain = preagg.grain
    if grain is None or grain.grain == "day" or grain.calendar != "jalali":
        return
    dim = preagg.time_dim
    qs = source(preagg)
    if window is not None:
        qs = qs.filter(date_range_q(dim.path, window[0], window[1], dim.is_date))
    expr = local_date(dim.path or dim.expr, dim.is_date)
    span = qs.using(using).aggregate(lo=Min(expr), hi=Max(expr))
    lo, hi = span["lo"], span["hi"]
    if lo is None:
        return
    if isinstance(lo, dt.datetime):
        lo, hi = lo.date(), hi.date()
    missing = table.missing_days(lo, hi)
    if missing:
        raise ReportError(
            409,
            "calendar_not_populated",
            f"The calendar table lacks {missing} day(s) between {lo} and {hi}; "
            "run `manage.py gozar_calendar`",
        )


def create_table(using: str, model) -> None:
    """``CREATE TABLE`` (+ indexes) for ``model`` from Django's schema editor, run
    directly: the editor's context manager refuses to run inside a transaction on
    SQLite, and builds may be inside one (a job run within a request, tests)."""
    connection = connections[using]
    editor = connection.schema_editor(collect_sql=True, atomic=False)
    editor.deferred_sql = []
    editor.create_model(model)
    statements = [*editor.collected_sql, *(str(s) for s in editor.deferred_sql)]
    with connection.cursor() as cursor:
        for sql in statements:
            cursor.execute(sql.rstrip().rstrip(";"))


def table_exists(using: str, name: str) -> bool:
    with connections[using].cursor() as cursor:
        return name in connections[using].introspection.table_names(cursor)


def drop_table(using: str, name: str) -> None:
    if name and table_exists(using, name):
        with connections[using].cursor() as cursor:
            cursor.execute(f"DROP TABLE {connections[using].ops.quote_name(name)}")


def table_size(using: str, name: str) -> int | None:
    """Bytes of a summary table and its indexes (PostgreSQL, SQLite with dbstat)."""
    connection = connections[using]
    try:
        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute("SELECT pg_total_relation_size(%s::regclass)", [name])
                return int(cursor.fetchone()[0])
            if connection.vendor == "sqlite":
                cursor.execute(
                    "SELECT SUM(pgsize) FROM dbstat WHERE name = %s OR name IN "
                    "(SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = %s)",
                    [name, name],
                )
                value = cursor.fetchone()[0]
                return int(value) if value is not None else None
    except Exception:  # dbstat isn't compiled into every SQLite
        logger.debug("No size for %s", name, exc_info=True)
    return None


# --- leases --------------------------------------------------------------------------


def acquire(row: Materialization) -> str:
    token = uuid.uuid4().hex
    now = timezone.now()
    expired = now - dt.timedelta(seconds=float(setting("PREAGG_BUILD_LEASE_SECONDS") or 3600))
    free = Q(building_token="") | Q(building_since__lt=expired) | Q(building_since__isnull=True)
    updated = (
        Materialization.objects.using(DEFAULT_DB_ALIAS)
        .filter(pk=row.pk)
        .filter(free)
        .update(building_token=token, building_since=now)
    )
    if not updated:
        raise BuildBusy(row.name)
    return token


def release(row: Materialization, token: str) -> None:
    Materialization.objects.using(DEFAULT_DB_ALIAS).filter(pk=row.pk, building_token=token).update(
        building_token="", building_since=None
    )


def without_failure(details: dict) -> dict:
    return {k: v for k, v in (details or {}).items() if k != "failed_at"}


def clear_records(row: Materialization, changes: int) -> None:
    """Forget the change records the refresh covered — unless more arrived meanwhile
    (then they stay, and the copy stays stale through its older versions)."""
    Materialization.objects.using(DEFAULT_DB_ALIAS).filter(pk=row.pk, changes=changes).update(
        stale_since=None, dirty_from=None, dirty_to=None, needs_rebuild=False
    )


# --- refresh -------------------------------------------------------------------------


def choose_mode(preagg: PreAggregation, row: Materialization, mode: str) -> str:
    """``rebuild`` or ``incremental`` for a requested ``auto``/``incremental``."""
    if mode == "rebuild":
        return "rebuild"
    if (
        row.status != Materialization.Status.READY
        or row.definition != preagg.definition
        or not row.table
        or preagg.grain is None
        or preagg.incremental_window is None
        and mode == "auto"
        or row.needs_rebuild
    ):
        return "rebuild"
    found = states.freshness(row)
    if not found.fresh and row.changes == row.details.get("changes_at_refresh"):
        return "rebuild"  # stale with no record of what changed
    return "incremental"


def refresh(preagg: PreAggregation, mode: str = "auto", today: dt.date | None = None) -> dict:
    """Bring ``preagg`` up to date; returns what was done. ``mode`` is ``auto``
    (incremental when declared and possible), ``incremental`` or ``rebuild``."""
    row = states.ensure_state(Kind.PREAGGREGATION, preagg.key, preagg.semantic.name)
    token = acquire(row)
    started = time.monotonic()
    try:
        row.refresh_from_db()
        chosen = choose_mode(preagg, row, mode)
        if chosen == "rebuild":
            outcome = rebuild(preagg, row, token)
        else:
            outcome = incremental(preagg, row, token, today)
    except Exception as exc:
        row.refresh_from_db()
        Materialization.objects.using(DEFAULT_DB_ALIAS).filter(pk=row.pk).update(
            last_error=f"{type(exc).__name__}: {exc}"[:2000],
            details={**row.details, "failed_at": timezone.now().isoformat()},
            **({"status": Materialization.Status.FAILED} if not row.table else {}),
        )
        raise
    finally:
        release(row, token)
    outcome["duration_ms"] = int((time.monotonic() - started) * 1000)
    Materialization.objects.using(DEFAULT_DB_ALIAS).filter(pk=row.pk).update(
        duration_ms=outcome["duration_ms"]
    )
    return outcome


def rebuild(preagg: PreAggregation, row: Materialization, token: str) -> dict:
    using = database()
    labels = dependencies(preagg, using)
    versions = cache.versions(labels)  # before reading anything
    changes = row.changes
    check_calendar(preagg, using)
    generation = row.generation + 1
    name = preagg.table_name(generation)
    model = preagg.model_for(name)
    drop_table(using, name)  # a leftover of a failed attempt
    create_table(using, model)
    try:
        with transaction.atomic(using=using):
            rows = insert_select(preagg, model, preagg.build_queryset(source(preagg)), using)
    except Exception:
        drop_table(using, name)
        raise
    previous = row.table
    now = timezone.now()
    updated = (
        Materialization.objects.using(DEFAULT_DB_ALIAS)
        .filter(pk=row.pk, building_token=token)
        .update(
            semantic_model=preagg.semantic.name,
            definition=preagg.definition,
            table=name,
            generation=generation,
            status=Materialization.Status.READY,
            row_count=rows,
            size_bytes=table_size(using, name),
            built_at=now,
            refreshed_at=now,
            refresh_mode="rebuild",
            source_versions=versions,
            sources=states.encode_labels(labels),
            last_error="",
            details={
                **without_failure(row.details),
                "changes_at_refresh": changes,
                "database": using,
            },
        )
    )
    if not updated:  # our lease expired and someone else took over
        drop_table(using, name)
        raise BuildBusy(row.name)
    clear_records(row, changes)
    if previous and previous != name:
        drop_table(using, previous)
    drop_orphans(using, preagg)
    return {"name": preagg.key, "mode": "rebuild", "rows": rows, "table": name}


def incremental(
    preagg: PreAggregation, row: Materialization, token: str, today: dt.date | None
) -> dict:
    using = database()
    labels = states.labels_of(row) or dependencies(preagg, using)
    versions = cache.versions(labels)
    changes = row.changes
    today = today or periods.today()
    start = today - dt.timedelta(days=int(preagg.incremental_window or 0))
    end = today
    if row.dirty_from is not None:
        start = min(start, row.dirty_from)
    if row.dirty_to is not None:
        end = max(end, row.dirty_to)
    start, end = preagg.window_for(start, end)
    check_calendar(preagg, using, (start, end))
    model = preagg.model_for(row.table)
    with transaction.atomic(using=using):
        stale = model._default_manager.using(using).filter(
            Q(**{f"{TIME_COLUMN}__range": (start, end)}) | Q(**{f"{TIME_COLUMN}__isnull": True})
        )
        deleted = stale.delete()[0]
        inserted = insert_select(
            preagg, model, preagg.build_queryset(source(preagg), (start, end)), using
        )
    total = model._default_manager.using(using).count()
    now = timezone.now()
    Materialization.objects.using(DEFAULT_DB_ALIAS).filter(pk=row.pk, building_token=token).update(
        row_count=total,
        size_bytes=table_size(using, row.table),
        refreshed_at=now,
        refresh_mode="incremental",
        source_versions=versions,
        sources=states.encode_labels(labels),
        last_error="",
        details={
            **without_failure(row.details),
            "changes_at_refresh": changes,
            "window": [start.isoformat(), end.isoformat()],
        },
    )
    clear_records(row, changes)
    return {
        "name": preagg.key,
        "mode": "incremental",
        "window": [start.isoformat(), end.isoformat()],
        "deleted": deleted,
        "inserted": inserted,
        "rows": total,
        "table": row.table,
    }


def drop_orphans(using: str, preagg: PreAggregation | None = None) -> list[str]:
    """Drop summary tables no state row points at (old definitions, failed builds);
    with ``preagg``, only that pre-aggregation's."""
    referenced = set(
        Materialization.objects.using(DEFAULT_DB_ALIAS)
        .filter(kind=Kind.PREAGGREGATION)
        .exclude(table="")
        .values_list("table", flat=True)
    )
    with connections[using].cursor() as cursor:
        names = connections[using].introspection.table_names(cursor)
    pattern = None
    if preagg is not None:
        slug = re.escape(f"{preagg.semantic.name[:16]}_{preagg.name[:16]}")
        pattern = re.compile(rf"^{TABLE_PREFIX}{slug}_[0-9a-f]{{8}}_\d+$")
    dropped = []
    for name in names:
        if not name.startswith(TABLE_PREFIX) or name in referenced:
            continue
        if pattern is not None and not pattern.match(name):
            continue
        drop_table(using, name)
        dropped.append(name)
    return dropped


def due(preagg: PreAggregation, row: Materialization | None, now=None) -> str | None:
    """The refresh a pre-aggregation needs now (``rebuild``/``auto``), if any."""
    now = now or timezone.now()
    failed_at = (row.details or {}).get("failed_at") if row is not None else None
    if failed_at:  # don't retry a failing build more often than it would refresh
        since = (now - dt.datetime.fromisoformat(failed_at)).total_seconds()
        if since < preagg.setting("REFRESH_EVERY"):
            return None
    if row is None or row.status != Materialization.Status.READY or not row.table:
        return "rebuild"
    if row.definition != preagg.definition:
        return "rebuild"
    every = preagg.setting("REBUILD_EVERY")
    if every and row.built_at and (now - row.built_at).total_seconds() >= every:
        return "rebuild"
    if states.freshness(row, now).fresh:
        return None
    wait = preagg.setting("REFRESH_EVERY")
    if row.refreshed_at and (now - row.refreshed_at).total_seconds() < wait:
        return None
    return "auto"


def refresh_due(now=None) -> list[dict[str, Any]]:
    """Refresh every pre-aggregation that is due (``gozar_worker``, ``--due``)."""
    from bi_admin.scale.preaggregations import all_preaggregations

    rows = states.states(Kind.PREAGGREGATION)
    out = []
    for preagg in all_preaggregations():
        mode = due(preagg, rows.get(preagg.key), now)
        if mode is None:
            continue
        try:
            out.append(refresh(preagg, mode))
        except BuildBusy:
            out.append({"name": preagg.key, "mode": mode, "skipped": "busy"})
        except Exception as exc:
            logger.exception("Refreshing %s failed", preagg.key)
            out.append({"name": preagg.key, "mode": mode, "error": str(exc)})
    return out
