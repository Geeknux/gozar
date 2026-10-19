"""Send due scheduled e-mail reports (run it from cron, e.g. every 5 minutes)."""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from bi_admin import scheduling
from bi_admin.models import Schedule


class Command(BaseCommand):
    help = (
        "Send every enabled Gozar e-mail schedule that is due. Each recipient gets the "
        "report run as them (their scope, language and calendar)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--schedule",
            type=int,
            action="append",
            help="send this schedule now, due or not (repeatable); the timetable is kept",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="list what is due without sending"
        )

    def handle(self, *args, schedule=None, dry_run=False, **options):
        now = timezone.now()
        if schedule:
            found = list(Schedule.objects.filter(pk__in=schedule))
            missing = sorted(set(schedule) - {s.pk for s in found})
            if missing:
                raise CommandError(f"Unknown schedule(s): {', '.join(map(str, missing))}")
            if dry_run:
                for s in found:
                    self.stdout.write(f"Would send {s.pk}: {s}")
                return
            deliveries = [d for s in found for d in scheduling.deliver(s, trigger="manual")]
        elif dry_run:
            due = Schedule.objects.filter(enabled=True, next_run_at__lte=now)
            for s in due:
                self.stdout.write(f"Due {s.pk}: {s} (since {s.next_run_at.isoformat()})")
            self.stdout.write(f"{len(due)} schedule(s) due.")
            return
        else:
            deliveries = scheduling.send_due(now)
        for d in deliveries:
            line = f"{d.schedule_name} → {d.username} <{d.email}>: {d.status}"
            self.stdout.write(line + (f" ({d.error})" if d.error else ""))
        sent = sum(1 for d in deliveries if d.status == "sent")
        self.stdout.write(self.style.SUCCESS(f"Sent {sent} of {len(deliveries)} e-mail(s)."))
