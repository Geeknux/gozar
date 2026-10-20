"""Run queued background report jobs (``GOZAR["JOB_BACKEND"]`` = DatabaseBackend)
and, unless ``GOZAR["WORKER_SENDS_SCHEDULES"]`` is off, send due e-mail schedules;
unless ``GOZAR["WORKER_REFRESHES"]`` is off, refresh due pre-aggregations and
columnar copies (M7)."""

import datetime as dt
import time

from django.core.management.base import BaseCommand

from bi_admin import scheduling
from bi_admin.conf import setting
from bi_admin.execution import jobs
from bi_admin.scale import columnar, refresh


class Command(BaseCommand):
    help = "Execute queued Gozar report jobs; runs until stopped unless --burst is given."

    def add_arguments(self, parser):
        parser.add_argument(
            "--burst", action="store_true", help="exit once the queue is empty (cron, tests)"
        )
        parser.add_argument("--sleep", type=float, default=1.0, help="idle poll interval (s)")
        parser.add_argument("--max-jobs", type=int, default=0, help="exit after N jobs (0: ∞)")
        parser.add_argument(
            "--keep-days",
            type=float,
            default=7,
            help="delete finished jobs older than this many days (0: keep all)",
        )

    def handle(self, *args, burst, sleep, max_jobs, keep_days, **options):
        done = 0
        last_purge: float | None = None
        last_recovery: float | None = None
        last_schedules: float | None = None
        last_refresh: float | None = None
        while True:
            if setting("WORKER_REFRESHES") and (
                last_refresh is None or time.monotonic() - last_refresh > 30
            ):
                for o in [*refresh.refresh_due(), *columnar.sync_due()]:
                    state = o.get("error") or o.get("skipped") or f"{o.get('rows')} rows"
                    self.stdout.write(f"Refresh {o['name']} ({o.get('mode')}): {state}")
                last_refresh = time.monotonic()
            if setting("WORKER_SENDS_SCHEDULES") and (
                last_schedules is None or time.monotonic() - last_schedules > 30
            ):
                for d in scheduling.send_due():
                    self.stdout.write(f"E-mail {d.schedule_name} → {d.username}: {d.status}")
                last_schedules = time.monotonic()
            # Requeue (or fail) jobs whose worker died, about once per lease period.
            if last_recovery is None or time.monotonic() - last_recovery > jobs.lease_seconds():
                recovered = jobs.recover_stale()
                if any(recovered.values()):
                    self.stdout.write(
                        f"Stale jobs: {recovered['requeued']} requeued, "
                        f"{recovered['failed']} failed."
                    )
                last_recovery = time.monotonic()
            if keep_days and (last_purge is None or time.monotonic() - last_purge > 3600):
                purged = jobs.purge(dt.timedelta(days=keep_days))
                if purged:
                    self.stdout.write(f"Purged {purged} old job(s).")
                last_purge = time.monotonic()
            job = jobs.claim()
            if job is None:
                if burst:
                    break
                time.sleep(sleep)
                continue
            job = jobs.run_job(job)
            done += 1
            self.stdout.write(f"Job {job.pk} ({job.semantic_model}): {job.status}")
            if max_jobs and done >= max_jobs:
                break
        self.stdout.write(self.style.SUCCESS(f"Processed {done} job(s)."))
