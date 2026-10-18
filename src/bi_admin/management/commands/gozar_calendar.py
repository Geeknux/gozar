"""Populate the ``CalendarDay`` dimension table used for Jalali grouping."""

from django.core.management.base import BaseCommand, CommandError

from bi_admin.calendar import jalali, table


class Command(BaseCommand):
    help = (
        "Fill the Gozar calendar table (one row per day, Gregorian + Jalali). "
        "Dates may be Gregorian (2026-03-21) or Jalali (1405/01/01). Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument("--start", help="first day (default: 15 Jalali years back)")
        parser.add_argument("--end", help="last day (default: end of the year 5 years ahead)")
        parser.add_argument(
            "--clear", action="store_true", help="delete existing rows in the range first"
        )

    def handle(self, *args, start=None, end=None, clear=False, **options):
        from bi_admin.models import CalendarDay

        default_start, default_end = table.default_range()
        try:
            first = jalali.parse_date(start) if start else default_start
            last = jalali.parse_date(end) if end else default_end
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        if last < first:
            raise CommandError("--end is before --start")
        if clear:
            CalendarDay.objects.filter(date__range=(first, last)).delete()
        created = table.populate(first, last)
        self.stdout.write(
            self.style.SUCCESS(
                f"Calendar {first} → {last} "
                f"({jalali.bucket_label(first, 'day', 'jalali')} → "
                f"{jalali.bucket_label(last, 'day', 'jalali')}): {created} days added."
            )
        )
