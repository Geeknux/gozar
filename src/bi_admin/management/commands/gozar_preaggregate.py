"""Build and refresh pre-aggregation tables (``bi_admin.scale``)."""

from django.core.management.base import BaseCommand, CommandError

from bi_admin.models import Materialization
from bi_admin.scale import refresh, state
from bi_admin.scale.preaggregations import all_preaggregations


class Command(BaseCommand):
    help = (
        "Rebuild or incrementally refresh Gozar pre-aggregations. Without names, every "
        "pre-aggregation; --due refreshes only those whose schedule says so."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "names", nargs="*", help="<semantic>.<name> or a unique <name> (default: all)"
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--rebuild", action="store_true", help="always rebuild from scratch")
        mode.add_argument(
            "--incremental",
            action="store_true",
            help="recompute the recent window and the days writes touched",
        )
        mode.add_argument("--due", action="store_true", help="only what the schedule says is due")
        mode.add_argument("--list", action="store_true", help="show the state, change nothing")
        parser.add_argument(
            "--drop-orphans",
            action="store_true",
            help="also drop summary tables no pre-aggregation uses any more",
        )

    def select(self, names):
        preaggs = all_preaggregations()
        if not names:
            return preaggs
        out = []
        for name in names:
            found = [p for p in preaggs if name in (p.key, p.name)]
            if len(found) != 1:
                known = ", ".join(p.key for p in preaggs) or "none"
                raise CommandError(
                    f"Unknown or ambiguous pre-aggregation {name!r} (known: {known})"
                )
            out.append(found[0])
        return out

    def handle(self, *args, names, rebuild, incremental, due, list, drop_orphans, **options):
        preaggs = self.select(names)
        if list:
            rows = state.states(Materialization.Kind.PREAGGREGATION)
            for p in preaggs:
                row = rows.get(p.key)
                if row is None:
                    self.stdout.write(f"{p.key}: not built")
                    continue
                fresh = state.freshness(row)
                current = "" if row.definition == p.definition else " (definition changed)"
                self.stdout.write(
                    f"{p.key}: {row.status}{current}, {row.row_count} rows in {row.table}, "
                    f"refreshed {row.refreshed_at:%Y-%m-%d %H:%M} ({row.refresh_mode}), "
                    f"{'fresh' if fresh.fresh else 'stale'}, {row.hits} hits"
                    if row.refreshed_at
                    else f"{p.key}: {row.status}"
                )
            return
        if due:
            outcomes = refresh.refresh_due()
        else:
            mode = "rebuild" if rebuild else "incremental" if incremental else "auto"
            outcomes = []
            for p in preaggs:
                try:
                    outcomes.append(refresh.refresh(p, mode))
                except refresh.BuildBusy:
                    outcomes.append({"name": p.key, "skipped": "busy"})
        for o in outcomes:
            if "error" in o:
                self.stderr.write(f"{o['name']}: {o['error']}")
            elif "skipped" in o:
                self.stdout.write(f"{o['name']}: skipped ({o['skipped']})")
            else:
                window = f" {o['window'][0]}…{o['window'][1]}" if "window" in o else ""
                self.stdout.write(
                    f"{o['name']}: {o['mode']}{window}, {o['rows']} rows, {o['duration_ms']} ms"
                )
        if drop_orphans:
            for name in refresh.drop_orphans(refresh.database()):
                self.stdout.write(f"Dropped {name}")
        if any("error" in o for o in outcomes):
            raise CommandError("Some pre-aggregations failed")
        self.stdout.write(self.style.SUCCESS(f"{len(outcomes)} pre-aggregation(s) processed."))
