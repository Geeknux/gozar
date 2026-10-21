"""Send a test alert through the configured channels."""

from django.core.management.base import BaseCommand, CommandError

from bi_admin import alerts
from bi_admin.alerts import outbox, sources, telegram


class Command(BaseCommand):
    help = "Queue a test alert and, unless --queue-only, send it right away."

    def add_arguments(self, parser):
        parser.add_argument("--message", default="", help="text of the alert")
        parser.add_argument("--severity", default="info", choices=sources.SEVERITIES)
        parser.add_argument(
            "--queue-only", action="store_true", help="leave sending to gozar_worker"
        )

    def handle(self, *args, message, severity, queue_only, **options):
        cfg = telegram.config()
        for problem in cfg.problems:
            self.stderr.write(self.style.WARNING(problem["message"]))
        alert = alerts.send_test(message=message, severity=severity)
        if alert is None:
            raise CommandError("Not recorded: alerts or the 'test' source are switched off.")
        self.stdout.write(f"Alert #{alert.pk} recorded ({alert.get_delivery_display()}).")
        if not cfg.enabled:
            self.stdout.write("No channel is configured, so nothing was sent.")
            return
        if queue_only:
            self.stdout.write("Queued; gozar_worker sends it.")
            return
        counts = outbox.deliver_due()
        alert.refresh_from_db()
        self.stdout.write(
            f"Sent {counts['sent']}, retrying {counts['retry']}, failed {counts['failed']}: "
            f"{alert.get_delivery_display()}."
        )
        if counts["failed"] or counts["retry"]:
            errors = {d.last_error for d in alert.deliveries.all() if d.last_error}
            for error in errors:
                self.stderr.write(self.style.ERROR(error))
