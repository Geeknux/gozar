"""Alert maintenance for cron: the periodic checks and the outbox without a
running ``gozar_worker`` — and the watchdog that notices a dead one."""

from django.core.management.base import BaseCommand

from bi_admin.alerts import monitors, outbox
from bi_admin.alerts import pipeline as alert_pipeline


class Command(BaseCommand):
    help = (
        "Run the alert checks (worker heartbeat, staleness, database, disks), "
        "optionally Django's system checks, and send queued alert messages."
    )

    def add_arguments(self, parser):
        parser.add_argument("--no-check", action="store_true", help="skip the periodic checks")
        parser.add_argument(
            "--system-checks", action="store_true", help="also run Django's system checks"
        )
        parser.add_argument("--no-deliver", action="store_true", help="don't send the outbox")
        parser.add_argument("--purge", action="store_true", help="delete alerts past KEEP_DAYS")

    def handle(self, *args, no_check, system_checks, no_deliver, purge, **options):
        if not no_check:
            monitors.run(system_checks=system_checks)
            self.stdout.write("Checks done.")
        if not no_deliver:
            counts = outbox.deliver_due()
            self.stdout.write(
                f"Alert messages: {counts['sent']} sent, {counts['retry']} to retry, "
                f"{counts['failed']} failed."
            )
        if purge:
            self.stdout.write(f"Purged {alert_pipeline.purge()} old alert(s).")
