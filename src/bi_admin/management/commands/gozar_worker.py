"""Run queued background report jobs (``GOZAR["JOB_BACKEND"]`` = DatabaseBackend)."""

import datetime as dt
import time

from django.core.management.base import BaseCommand

from bi_admin.execution import jobs


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
        while True:
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
