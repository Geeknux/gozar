"""The System page's Scale section: pre-aggregations, columnar copies and the
maintenance jobs that refresh them (``bi_admin.scale``)."""

from __future__ import annotations

from typing import Any

from bi_admin.execution import jobs
from bi_admin.models import Materialization, ReportJob
from bi_admin.scale import columnar, refresh
from bi_admin.scale import state as states
from bi_admin.scale.preaggregations import all_preaggregations

RECENT_JOBS = 10


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def state_payload(row: Materialization | None, preagg=None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = {
        "status": row.status,
        "table": row.table,
        "generation": row.generation,
        "rows": row.row_count,
        "size_bytes": row.size_bytes,
        "built_at": _iso(row.built_at),
        "refreshed_at": _iso(row.refreshed_at),
        "refresh_mode": row.refresh_mode,
        "duration_ms": row.duration_ms,
        "freshness": states.freshness(row).as_dict() if row.source_versions else None,
        "needs_rebuild": row.needs_rebuild,
        "dirty_from": _iso(row.dirty_from),
        "dirty_to": _iso(row.dirty_to),
        "hits": row.hits,
        "last_hit_at": _iso(row.last_hit_at),
        "last_error": row.last_error,
        "building": bool(row.building_token),
        "window": (row.details or {}).get("window"),
    }
    if preagg is not None:
        out["definition_current"] = row.definition == preagg.definition
    return out


def preaggregations() -> list[dict[str, Any]]:
    rows = states.states(Materialization.Kind.PREAGGREGATION)
    out = []
    for preagg in all_preaggregations():
        row = rows.get(preagg.key)
        item = preagg.describe()
        item["state"] = state_payload(row, preagg)
        item["due"] = refresh.due(preagg, row)
        out.append(item)
    return out


def backends() -> list[dict[str, Any]]:
    used = {b.name for b in columnar.in_use()}
    out = []
    for name in columnar.names():
        try:
            backend = columnar.get(name)
        except ImportError as exc:  # a misconfigured dotted path
            out.append({"name": name, "label": name, "error": str(exc), "in_use": False})
            continue
        if backend is None:
            continue
        out.append({**backend.status(), "in_use": name in used})
    return out


def recent_jobs() -> list[dict[str, Any]]:
    maintenance = ReportJob.objects.exclude(kind=ReportJob.Kind.REPORT).select_related("user")
    return [jobs.payload(j) for j in maintenance.order_by("-created_at")[:RECENT_JOBS]]


def overview() -> dict[str, Any]:
    from bi_admin.conf import setting

    return {
        "enabled": bool(setting("PREAGGREGATIONS")),
        "database": refresh.database(),
        "worker_refreshes": bool(setting("WORKER_REFRESHES")),
        "preaggregations": preaggregations(),
        "columnar": backends(),
        "jobs": recent_jobs(),
    }
