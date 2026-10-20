"""Copy the data of semantic models with a columnar engine (e.g. DuckDB) into it."""

from django.core.management.base import BaseCommand, CommandError

from bi_admin.scale import columnar


class Command(BaseCommand):
    help = (
        "Sync the columnar engines' copies from the reports database. Without names, every "
        "engine some semantic model uses; --due syncs only stale ones past SYNC_EVERY."
    )

    def add_arguments(self, parser):
        parser.add_argument("names", nargs="*", help="engine names, e.g. duckdb")
        parser.add_argument("--due", action="store_true", help="only copies that are due")

    def handle(self, *args, names, due, **options):
        if due:
            outcomes = columnar.sync_due()
        else:
            backends = [columnar.get(n) for n in names] if names else columnar.in_use()
            if any(b is None for b in backends):
                raise CommandError(f"Unknown engine (known: {', '.join(columnar.names())})")
            if not backends:
                self.stdout.write("No semantic model uses a columnar engine.")
                return
            outcomes = []
            for backend in backends:
                try:
                    outcomes.append(backend.sync())
                except Exception as exc:
                    outcomes.append({"name": backend.name, "error": str(exc)})
        for o in outcomes:
            if "error" in o:
                self.stderr.write(f"{o['name']}: {o['error']}")
            else:
                tables = ", ".join(f"{t} {n}" for t, n in o["tables"].items())
                self.stdout.write(
                    f"{o['name']}: {o['rows']} rows in {o['duration_ms']} ms ({tables})"
                )
        if any("error" in o for o in outcomes):
            raise CommandError("Some syncs failed")
