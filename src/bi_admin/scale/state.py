"""Freshness of pre-aggregations and columnar copies (``bi_admin.Materialization``).

A copy is *fresh* while the data versions of every model it read
(``bi_admin.execution.cache``) are the ones captured before it read them; any
save, delete or ``cache.bump()`` since makes it stale. Stale copies answer only
within their ``max_staleness``; the lag is measured from ``stale_since``.

Writes also record what they touched, so incremental refreshes know which
buckets to recompute: a saved or deleted row of a pre-aggregation's own model
widens ``dirty_from``/``dirty_to`` by its local date (old and new, for a moved
row); anything else — a related model, a bulk write — sets ``needs_rebuild``.
These records are written after the writer's transaction commits (one
``UPDATE`` per write, only for models a copy reads), so a rolled-back write leaves
none. A stale copy without records (a rollback, an evicted cache version, a
write that only bumped the version) is treated as needing a full rebuild.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from django.db import DEFAULT_DB_ALIAS, DatabaseError, transaction
from django.db.models import F, Value
from django.db.models.functions import Coalesce, Greatest, Least
from django.db.models.signals import pre_save
from django.utils import timezone

from bi_admin.execution import cache
from bi_admin.models import Materialization

logger = logging.getLogger("bi_admin.scale")
Kind = Materialization.Kind
OLD_DATE = "_gozar_scale_old_dates"


def get_state(kind: str, name: str) -> Materialization | None:
    return Materialization.objects.using(DEFAULT_DB_ALIAS).filter(kind=kind, name=name).first()


def states(kind: str) -> dict[str, Materialization]:
    rows = Materialization.objects.using(DEFAULT_DB_ALIAS).filter(kind=kind)
    return {row.name: row for row in rows}


def ensure_state(kind: str, name: str, semantic_model: str = "") -> Materialization:
    row, _created = Materialization.objects.using(DEFAULT_DB_ALIAS).get_or_create(
        kind=kind, name=name, defaults={"semantic_model": semantic_model}
    )
    return row


def labels_of(row: Materialization) -> list[str]:
    return [label for label in row.sources.split(",") if label]


def encode_labels(labels) -> str:
    return "," + ",".join(sorted(set(labels))) + ","


@dataclass
class Freshness:
    fresh: bool
    stale_since: dt.datetime | None = None
    lag_seconds: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "fresh": self.fresh,
            "stale_since": self.stale_since.isoformat() if self.stale_since else None,
            "lag_seconds": round(self.lag_seconds, 3) if self.lag_seconds is not None else None,
        }


def freshness(row: Materialization, now: dt.datetime | None = None) -> Freshness:
    """Compare the copy's captured data versions with the current ones."""
    labels = labels_of(row)
    if not row.source_versions or not labels:
        return Freshness(False, None, None)
    if cache.versions(labels) == row.source_versions:
        return Freshness(True)
    now = now or timezone.now()
    since = row.stale_since
    if since is None:  # stale without a record (rollback, evicted version): from now on
        Materialization.objects.using(DEFAULT_DB_ALIAS).filter(
            pk=row.pk, stale_since__isnull=True
        ).update(stale_since=now)
        since = now
    return Freshness(False, since, max(0.0, (now - since).total_seconds()))


def usable(row: Materialization, max_staleness: float, now=None) -> tuple[bool, Freshness]:
    found = freshness(row, now)
    if found.fresh:
        return True, found
    if found.lag_seconds is None:
        return False, found
    return max_staleness > 0 and found.lag_seconds <= max_staleness, found


def record_hit(row: Materialization) -> None:
    Materialization.objects.using(DEFAULT_DB_ALIAS).filter(pk=row.pk).update(
        hits=F("hits") + 1, last_hit_at=timezone.now()
    )


# --- change records ------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """A copy whose records a write may update."""

    kind: str
    name: str
    labels: frozenset[str]  # every model it reads ("*" matches cache.bump_all)
    base: str = ""  # its own model's label
    time_attr: str | None = None  # a local field of ``base`` holding the bucketed time
    is_date: bool = False


_targets_cache: tuple[tuple, list[Target]] | None = None


def targets() -> list[Target]:
    """Every pre-aggregation and columnar copy (from the bound semantic models),
    recomputed when the semantic models are rebound or the engine settings change."""
    global _targets_cache
    from bi_admin.scale import columnar
    from bi_admin.scale.preaggregations import all_preaggregations

    preaggs = all_preaggregations()
    synced = columnar.synced_labels()
    key = (tuple(id(p) for p in preaggs), tuple(sorted((k, tuple(v)) for k, v in synced.items())))
    if _targets_cache is not None and _targets_cache[0] == key:
        return _targets_cache[1]
    out = [preagg_target(p) for p in preaggs]
    out += [Target(Kind.COLUMNAR, name, frozenset(labels)) for name, labels in synced.items()]
    _targets_cache = (key, out)
    return out


def preagg_target(preagg) -> Target:
    from bi_admin.discovery import model_label
    from bi_admin.scale import paths

    sem = preagg.semantic
    labels = {sem.model_label}
    exprs = [d.expr for d in preagg.dims.values()]
    exprs += [c.path for c in preagg.columns if c.role == "scope"]
    exprs += [sem.measures[k].expr for k in preagg.measure_keys]
    time_attr = None
    if preagg.time_dim is not None:
        exprs.append(preagg.time_dim.expr)
        path = preagg.time_dim.path
        if path is not None and "__" not in path:
            time_attr = sem.model._meta.get_field(path).attname
        if preagg.grain.calendar == "jalali" and preagg.grain.grain != "day":
            labels.add("bi_admin.calendarday")
    for expr in exprs:
        for found in paths.expression_paths(sem.model, expr):
            for hop in paths.walk(sem.model, found).hops:
                labels.add(model_label(hop.target))
    return Target(
        Kind.PREAGGREGATION,
        preagg.key,
        frozenset(labels),
        sem.model_label,
        time_attr if preagg.incremental_window is not None else None,
        bool(preagg.time_dim and preagg.time_dim.is_date),
    )


def _local_date(value, is_date: bool) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None


def _on_pre_save(sender, instance, raw=False, using=None, update_fields=None, **kwargs) -> None:
    """Remember the bucketed time an existing row had, so moving it marks both days."""
    if raw or instance._state.adding or instance.pk is None:
        return
    try:
        attrs = _time_attrs().get(cache._label(sender))
    except Exception:  # an unbindable project must not break saving
        return
    if not attrs:
        return
    wanted = [a for a in attrs if update_fields is None or a.removesuffix("_id") in update_fields]
    if not wanted:
        return
    old = sender._base_manager.using(using).filter(pk=instance.pk).values(*wanted).first()
    if old is not None:
        instance.__dict__[OLD_DATE] = old


def _time_attrs() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for t in targets():
        if t.time_attr:
            found.setdefault(t.base, set()).add(t.time_attr)
    return found


def on_change(labels: set[str], changes, using: str | None) -> None:
    """``cache.listeners`` hook: record what a write touched, after it commits."""
    try:
        found = targets()
    except Exception:  # binding failures surface in system checks, not in saves
        logger.debug("Skipping change records: semantic models don't bind", exc_info=True)
        return
    marks: list[tuple[Target, bool, set[dt.date]]] = []
    for target in found:
        hit = labels & target.labels
        if not hit and "*" not in labels:
            continue
        if changes is None or not target.time_attr or hit != {target.base}:
            marks.append((target, True, set()))
            continue
        days: set[dt.date] = set()
        for change in changes:
            if cache._label(change.model) != target.base:
                continue
            value = getattr(change.instance, target.time_attr, None)
            days.add(_local_date(value, target.is_date))
            old = change.instance.__dict__.get(OLD_DATE, {})
            if target.time_attr in old:
                days.add(_local_date(old[target.time_attr], target.is_date))
        marks.append((target, False, {d for d in days if d is not None}))
    if not marks:
        return
    now = timezone.now()
    transaction.on_commit(lambda: write_marks(marks, now), using=using)


def write_marks(marks, now: dt.datetime) -> None:
    for target, rebuild, days in marks:
        update: dict[str, Any] = {
            "stale_since": Coalesce(F("stale_since"), Value(now)),
            "changes": F("changes") + 1,
        }
        if rebuild:
            update["needs_rebuild"] = True
        elif days:
            lo, hi = min(days), max(days)
            update["dirty_from"] = Least(Coalesce(F("dirty_from"), Value(lo)), Value(lo))
            update["dirty_to"] = Greatest(Coalesce(F("dirty_to"), Value(hi)), Value(hi))
        try:
            with transaction.atomic(using=DEFAULT_DB_ALIAS):
                Materialization.objects.using(DEFAULT_DB_ALIAS).filter(
                    kind=target.kind, name=target.name
                ).update(**update)
        except DatabaseError:  # e.g. while migrating, before the table exists
            logger.debug("Could not record a change for %s", target.name, exc_info=True)


def connect() -> None:
    if on_change not in cache.listeners:
        cache.listeners.append(on_change)
    pre_save.connect(_on_pre_save, dispatch_uid="gozar_scale_pre_save")
