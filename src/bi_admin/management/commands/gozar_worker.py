"""Run queued background report jobs (``GOZAR["JOB_BACKEND"]`` = DatabaseBackend)
and, unless ``GOZAR["WORKER_SENDS_SCHEDULES"]`` is off, send due e-mail schedules;
unless ``GOZAR["WORKER_REFRESHES"]`` is off, refresh due pre-aggregations and
columnar copies (M7). With alerts on, it also records a heartbeat, sends the
alert outbox, runs the periodic alert checks (and Django's system checks at start
and every ``SYSTEM_CHECKS_EVERY``) and, in interactive mode, polls Telegram."""

import datetime as dt
import signal
import threading
import time

from django.core.management.base import BaseCommand
from django.utils.translation import gettext as _

from bi_admin import scheduling
from bi_admin.alerts import bot, monitors, outbox
from bi_admin.alerts import pipeline as alert_pipeline
from bi_admin.alerts.conf import alert_setting, telegram_setting
from bi_admin.conf import setting
from bi_admin.execution import jobs
from bi_admin.scale import columnar, refresh

HEARTBEAT_EVERY = 15  # seconds
OUTBOX_EVERY = 5


def _terminate(signum, frame):
    raise SystemExit(0)


class Every:
    """``due()`` is true on the first call and then once per ``seconds``."""

    def __init__(self, seconds):
        self.seconds, self.last = seconds, None

    def due(self) -> bool:
        now = time.monotonic()
        if self.last is None or now - self.last > float(
            self.seconds() if callable(self.seconds) else self.seconds
        ):
            self.last = now
            return True
        return False


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
        previous = None
        if threading.current_thread() is threading.main_thread():
            # `docker stop`/systemd: leave through the finally below (heartbeat → stopped)
            previous = signal.signal(signal.SIGTERM, _terminate)
        alerting = bool(alert_setting("ENABLED"))
        name = monitors.worker_name()
        poller = None
        if alerting:
            monitors.beat(name, started=True)
            if telegram_setting("POLL_IN_WORKER"):
                poller = bot.start_thread()
                if poller:
                    self.stdout.write("Polling Telegram for bot commands.")
        clean = False
        try:
            done = self.loop(burst, sleep, max_jobs, keep_days, alerting, name)
            clean = True
        except (KeyboardInterrupt, SystemExit):
            clean = True
            raise
        except Exception as exc:
            if alerting:
                alert_pipeline.raise_alert(
                    "worker",
                    "crashed",
                    "critical",
                    _("gozar_worker crashed"),
                    f"{type(exc).__name__}: {exc}",
                    {"worker": name},
                )
            raise
        finally:
            if poller:
                poller[1].set()
            if alerting and clean:
                monitors.stopped(name)
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)
        self.stdout.write(self.style.SUCCESS(f"Processed {done} job(s)."))

    def loop(self, burst, sleep, max_jobs, keep_days, alerting, name) -> int:
        done = 0
        last_purge: float | None = None
        last_recovery: float | None = None
        last_schedules: float | None = None
        last_refresh: float | None = None
        heartbeat = Every(HEARTBEAT_EVERY)
        sends = Every(OUTBOX_EVERY)
        checks = Every(lambda: alert_setting("CHECK_EVERY") or 300)
        system_checks = Every(lambda: alert_setting("SYSTEM_CHECKS_EVERY") or 21_600)
        while True:
            if alerting:
                if heartbeat.due():
                    monitors.beat(name)
                if checks.due():
                    monitors.run(system_checks=system_checks.due())
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
                if alerting:
                    alert_pipeline.purge()
                last_purge = time.monotonic()
            if alerting and sends.due():
                sent = outbox.deliver_due()
                if any(sent.values()):
                    self.stdout.write(
                        f"Alert messages: {sent['sent']} sent, {sent['retry']} to retry, "
                        f"{sent['failed']} failed."
                    )
            job = jobs.claim()
            if job is None:
                if burst:
                    if alerting:
                        outbox.deliver_due()  # whatever the last jobs raised
                    break
                time.sleep(sleep)
                continue
            job = jobs.run_job(job)
            done += 1
            self.stdout.write(f"Job {job.pk} ({job.semantic_model}): {job.status}")
            if max_jobs and done >= max_jobs:
                break
        return done
