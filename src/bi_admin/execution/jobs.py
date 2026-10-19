"""Background report jobs: a small pluggable interface over ``bi_admin.ReportJob``.

Reports run synchronously by default. With ``GOZAR["REPORT_ASYNC"] = True``, a
semantic model's ``run_async = True`` or ``POST /reports/run?mode=async``, the
API stores a job and returns ``202`` with its id; the SPA polls
``GET /reports/jobs/<id>`` and fetches ``/result`` when it is done.

``GOZAR["JOB_BACKEND"]`` decides how a stored job gets executed:

* ``bi_admin.execution.jobs.DatabaseBackend`` (default) — nothing happens at
  submit time; ``manage.py gozar_worker`` claims queued jobs from the table.
* ``bi_admin.execution.jobs.ImmediateBackend`` — runs the job inside the
  submitting request (tests, development).
* your own ``JobBackend`` subclass, e.g. one whose ``submit`` enqueues a Celery
  task that calls ``execute(job_id)``.

A job runs as the submitting user, with the language and calendar they had at
submission: permissions, visibility and row rules are evaluated for them when
the job executes, and the run is audited with a link to the job.

Leases: claiming a job stamps a random lease token, ``attempts + 1`` and a
heartbeat, which a background thread refreshes every third of
``GOZAR["JOB_LEASE_SECONDS"]`` while the job runs. ``recover_stale()`` (run by
``gozar_worker``) requeues running jobs whose heartbeat is older than the lease
— their worker died — until ``GOZAR["JOB_MAX_ATTEMPTS"]``, then fails them with
``worker_lost``. Every update is conditional on the lease token, so a worker
presumed dead can never overwrite the outcome of a later attempt.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import uuid
from collections.abc import Callable
from typing import Any

from django.db import DEFAULT_DB_ALIAS, connections
from django.db.models import F
from django.utils import timezone, translation
from django.utils.module_loading import import_string
from django.utils.translation import gettext as _

from bi_admin import preferences
from bi_admin.conf import setting
from bi_admin.models import ReportJob

logger = logging.getLogger("bi_admin.jobs")
Status = ReportJob.Status


class JobRequest:
    """The parts of an ``HttpRequest`` the engine reads, rebuilt for a worker."""

    def __init__(self, user, language: str, calendar: str):
        self.user = user
        self.META: dict[str, Any] = {}
        self.COOKIES: dict[str, str] = {}
        self.GET: dict[str, str] = {}
        self.LANGUAGE_CODE = language
        self._gozar_preferences = preferences.Preferences(language=language, calendar=calendar)


class JobBackend:
    """How a stored job gets executed. ``submit`` runs right after the job row is
    created (inside the submitting request)."""

    def submit(self, job: ReportJob) -> None:
        raise NotImplementedError


class DatabaseBackend(JobBackend):
    """Leave the job queued; ``manage.py gozar_worker`` picks it up."""

    def submit(self, job: ReportJob) -> None:
        return None


class ImmediateBackend(JobBackend):
    """Execute the job at once, in the submitting process."""

    def submit(self, job: ReportJob) -> None:
        claimed = claim(job.pk)
        if claimed is not None:  # inside the request: no heartbeat thread needed
            run_job(claimed, heartbeat=False)


def get_backend() -> JobBackend:
    return import_string(setting("JOB_BACKEND"))()


def wants_async(semantic, mode: str) -> bool:
    """``mode`` is the client's choice: ``sync``, ``async`` or ``auto`` (the
    semantic model's ``run_async``, else ``GOZAR["REPORT_ASYNC"]``)."""
    if mode in ("sync", "async"):
        return mode == "async"
    if semantic.run_async is not None:
        return bool(semantic.run_async)
    return bool(setting("REPORT_ASYNC"))


def submit(spec, request, *, use_cache: bool = True) -> ReportJob:
    from bi_admin.semantic.base import active_language

    prefs = preferences.get(request)
    job = ReportJob.objects.create(
        user=request.user,
        semantic_model=spec.semantic_model,
        spec=spec.model_dump(mode="json"),
        options={"language": active_language(), "calendar": prefs.calendar, "cache": use_cache},
    )
    get_backend().submit(job)
    job.refresh_from_db()
    return job


def lease_seconds() -> float:
    return float(setting("JOB_LEASE_SECONDS") or 60)


def claim(job_id=None) -> ReportJob | None:
    """Atomically move a queued job (the given one, else the oldest) to running.
    A conditional UPDATE, so concurrent workers never run a job twice."""
    queued = ReportJob.objects.using(DEFAULT_DB_ALIAS).filter(status=Status.QUEUED)
    if job_id is not None:
        candidates = [job_id]
    else:
        candidates = list(queued.order_by("created_at").values_list("pk", flat=True)[:20])
    for pk in candidates:
        now = timezone.now()
        claimed = queued.filter(pk=pk).update(
            status=Status.RUNNING,
            started_at=now,
            heartbeat_at=now,
            lease=uuid.uuid4().hex,
            attempts=F("attempts") + 1,
        )
        if claimed:
            return ReportJob.objects.using(DEFAULT_DB_ALIAS).get(pk=pk)
    return None


def beat(job: ReportJob) -> bool:
    """Refresh the job's heartbeat; ``False`` once the lease is no longer ours."""
    return bool(
        ReportJob.objects.using(DEFAULT_DB_ALIAS)
        .filter(pk=job.pk, status=Status.RUNNING, lease=job.lease)
        .update(heartbeat_at=timezone.now())
    )


class Heartbeat:
    """Calls ``beat()`` every ``interval`` seconds in a daemon thread until stopped."""

    def __init__(self, job: ReportJob, interval: float, beat: Callable[[ReportJob], bool] = beat):
        self.job, self.interval, self._beat = job, interval, beat
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"gozar-job-{job.pk}", daemon=True)

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.interval):
                if not self._beat(self.job):
                    break
        except Exception:
            logger.exception("Heartbeat for report job %s failed", self.job.pk)
        finally:
            connections.close_all()  # this thread's own connections

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval)


def recover_stale(now: dt.datetime | None = None) -> dict[str, int]:
    """Requeue running jobs whose heartbeat is older than the lease (their worker
    died), or fail them with ``worker_lost`` once they used up their attempts."""
    now = now or timezone.now()
    cutoff = now - dt.timedelta(seconds=lease_seconds())
    max_attempts = int(setting("JOB_MAX_ATTEMPTS") or 1)
    stale = ReportJob.objects.using(DEFAULT_DB_ALIAS).filter(
        status=Status.RUNNING, heartbeat_at__lt=cutoff
    )
    counts = {"requeued": 0, "failed": 0}
    for job in stale.only("pk", "lease", "attempts", "options"):
        same = ReportJob.objects.using(DEFAULT_DB_ALIAS).filter(
            pk=job.pk, status=Status.RUNNING, lease=job.lease
        )
        if job.attempts < max_attempts:
            if same.update(status=Status.QUEUED, started_at=None, heartbeat_at=None, lease=""):
                counts["requeued"] += 1
                logger.warning("Requeued stale report job %s (attempt %s)", job.pk, job.attempts)
            continue
        language = (job.options or {}).get("language") or preferences.default_language()
        with translation.override(language):
            error = {
                "detail": _("The report job was lost: its worker stopped responding."),
                "code": "worker_lost",
                "attempts": job.attempts,
            }
        if same.update(
            status=Status.FAILED, finished_at=now, error=error, error_status=500, lease=""
        ):
            counts["failed"] += 1
            logger.error("Report job %s failed after %s lost attempts", job.pk, job.attempts)
    return counts


def execute(job_id) -> ReportJob | None:
    """Claim and run one job; ``None`` when it is no longer queued. This is what a
    custom backend's task runner (Celery, RQ…) calls."""
    job = claim(job_id)
    return run_job(job) if job is not None else None


def run_job(job: ReportJob, *, heartbeat: bool = True) -> ReportJob:
    """Run a claimed job; with ``heartbeat`` its lease is kept fresh meanwhile."""
    from bi_admin.engine import ReportError, SpecValidationError, run

    options = job.options or {}
    language = options.get("language") or preferences.default_language()
    calendar = options.get("calendar") or preferences.default_calendar()
    request = JobRequest(job.user, language, calendar)
    outcome: dict[str, Any] = {"status": Status.DONE}
    pulse = Heartbeat(job, lease_seconds() / 3) if heartbeat else None
    with translation.override(language):
        if pulse is not None:
            pulse.__enter__()
        try:
            result = run(job.spec, request, use_cache=options.get("cache", True), job=job)
            outcome["result"] = result.as_dict()
        except SpecValidationError as exc:
            outcome.update(status=Status.FAILED, error=exc.as_dict(), error_status=422)
        except ReportError as exc:
            outcome.update(status=Status.FAILED, error=exc.payload, error_status=exc.status)
        except Exception:
            logger.exception("Report job %s failed", job.pk)
            outcome.update(
                status=Status.FAILED,
                error={"detail": _("The report failed."), "code": "internal_error"},
                error_status=500,
            )
        finally:
            if pulse is not None:
                pulse.__exit__(None, None, None)
    # Only while the lease is ours: a job cancelled meanwhile stays cancelled, and one
    # requeued as stale belongs to its next attempt.
    ReportJob.objects.filter(pk=job.pk, status=Status.RUNNING, lease=job.lease).update(
        finished_at=timezone.now(), **outcome
    )
    job.refresh_from_db()
    return job


def cancel(job: ReportJob) -> bool:
    updated = ReportJob.objects.filter(
        pk=job.pk, status__in=[Status.QUEUED, Status.RUNNING]
    ).update(status=Status.CANCELLED, finished_at=timezone.now())
    job.refresh_from_db()
    return bool(updated)


def purge(older_than: dt.timedelta) -> int:
    """Delete finished jobs (and their stored results) older than ``older_than``."""
    cutoff = timezone.now() - older_than
    deleted, _counts = ReportJob.objects.filter(
        status__in=ReportJob.FINISHED, created_at__lt=cutoff
    ).delete()
    return deleted


def payload(job: ReportJob) -> dict[str, Any]:
    def iso(value):
        return value.isoformat() if value else None

    return {
        "id": str(job.pk),
        "status": job.status,
        "status_display": job.get_status_display(),
        "semantic_model": job.semantic_model,
        "spec": job.spec,
        "created_at": iso(job.created_at),
        "started_at": iso(job.started_at),
        "finished_at": iso(job.finished_at),
        "error": job.error,
        "error_status": job.error_status,
        "attempts": job.attempts,
    }
