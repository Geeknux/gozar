"""Time representative reports on the ORM path, pre-aggregations and DuckDB.

Seeds a large synthetic data set — by default a million orders — into a separate
database (``example_project/.benchmark/benchmark.sqlite3``, or a PostgreSQL
database with ``--postgres-db``), never the demo's ``db.sqlite3``. The command
re-runs itself in a child process pointed at that database, so nothing in this
process touches the demo data::

    python example_project/manage.py gozar_benchmark --orders 1000000

Each report runs ``--repeat`` times per engine with the result cache off; the
median is printed, together with whether every engine returned identical rows.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

CHILD = "GOZAR_BENCHMARK_CHILD"
ORDERS = "order_metrics"

REPORTS = [
    ("Revenue, orders, AOV — last 12 months (KPI)", {
        "measures": ["revenue", "orders", "aov"], "relative_period": "past_12_months"}),
    ("Revenue by city — last 3 months", {
        "measures": ["revenue"], "dimensions": ["city"], "relative_period": "last_3_months"}),
    ("Monthly revenue and orders — past 12 months", {
        "measures": ["revenue", "orders"], "time_grain": "month",
        "relative_period": "past_12_months"}),
    ("Monthly revenue vs last year (Jalali)", {
        "measures": ["revenue"], "time_grain": "month", "calendar": "jalali",
        "relative_period": "this_year", "compare_to": "same_period_last_year"}),
    ("Orders by province and status, by season (Jalali)", {
        "measures": ["orders", "paid_orders"], "dimensions": ["province", "status"],
        "time_grain": "season", "calendar": "jalali"}),
    ("AOV by city by week — last 3 months", {
        "measures": ["aov"], "dimensions": ["city"], "time_grain": "week",
        "relative_period": "last_3_months"}),
    ("Orders by status — all time", {"measures": ["orders"], "dimensions": ["status"]}),
    ("Top 10 customers by revenue — last year", {
        "measures": ["revenue", "orders"], "dimensions": ["customer"],
        "relative_period": "last_year", "sort": [{"field": "revenue", "direction": "desc"}],
        "limit": 10}),
    ("Distinct customers by month — past 12 months", {
        "measures": ["customers"], "time_grain": "month", "relative_period": "past_12_months"}),
    ("Revenue by city — Tehran region user (row rules)", {
        "measures": ["revenue", "orders"], "dimensions": ["city"],
        "relative_period": "past_12_months", "_user": "tehran"}),
]  # fmt: skip
STATUSES = ["paid"] * 6 + ["pending", "cancelled", "refunded"]
ENGINES = ("orm", "preaggregation", "duckdb")


class Command(BaseCommand):
    help = "Seed a large data set into a separate database and time reports per engine."

    def add_arguments(self, parser):
        parser.add_argument("--orders", type=int, default=1_000_000)
        parser.add_argument("--customers", type=int, default=20_000)
        parser.add_argument("--days", type=int, default=730, help="history length in days")
        parser.add_argument("--repeat", type=int, default=5, help="runs per report and engine")
        parser.add_argument("--seed", type=int, default=7)
        parser.add_argument(
            "--file",
            default=None,
            help="SQLite file (default: example_project/.benchmark/benchmark.sqlite3)",
        )
        parser.add_argument(
            "--postgres-db",
            default=None,
            help="benchmark on this PostgreSQL database instead (POSTGRES_* for the server); "
            "it is created when missing and must not be a database you use",
        )
        parser.add_argument("--reseed", action="store_true", help="drop and re-create the data")
        parser.add_argument(
            "--json", dest="as_json", action="store_true", help="also print the results as JSON"
        )

    # -- parent: start the child on the benchmark database --------------------------

    def handle(self, *args, **options):
        if os.environ.get(CHILD) == "1":
            return self.child(**options)
        root = Path(settings.BASE_DIR) / ".benchmark"
        root.mkdir(exist_ok=True)
        env = {
            **os.environ,
            CHILD: "1",
            "GOZAR_CACHE_DIR": str(root / "cache"),
            "GOZAR_DUCKDB_PATH": str(root / "benchmark.duckdb"),
            "GOZAR_DUCKDB": "1",
            "GOZAR_REPORTS_DATABASE": "",
            "GOZAR_MEDIA_ROOT": str(root / "media"),
        }
        if options["postgres_db"]:
            name = options["postgres_db"]
            if name in (os.environ.get("POSTGRES_DB", "gozar"), "postgres"):
                raise CommandError("Pick a database used only for the benchmark")
            env.update(GOZAR_DB="postgres", POSTGRES_DB=name)
            self.create_postgres(name)
        else:
            path = Path(options["file"] or root / "benchmark.sqlite3").resolve()
            if path == (Path(settings.BASE_DIR) / "db.sqlite3").resolve():
                raise CommandError("The benchmark never writes to the demo database")
            env.update(GOZAR_DB="sqlite", GOZAR_SQLITE_PATH=str(path))
        argv = [sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), "gozar_benchmark"]
        for name in ("orders", "customers", "days", "repeat", "seed"):
            argv.append(f"--{name}={options[name]}")
        argv += ["--reseed"] * options["reseed"] + ["--json"] * options["as_json"]
        completed = subprocess.run(argv, env=env, check=False)
        if completed.returncode:
            raise CommandError(f"The benchmark failed ({completed.returncode})")

    def create_postgres(self, name: str) -> None:
        import psycopg

        dsn = (
            f"host={os.environ.get('POSTGRES_HOST', 'localhost')} "
            f"port={os.environ.get('POSTGRES_PORT', '5432')} "
            f"user={os.environ.get('POSTGRES_USER', 'gozar')} "
            f"password={os.environ.get('POSTGRES_PASSWORD', 'gozar')} dbname=postgres"
        )
        with psycopg.connect(dsn, autocommit=True) as con:
            exists = con.execute("SELECT 1 FROM pg_database WHERE datname = %s", [name]).fetchone()
            if not exists:
                con.execute(f'CREATE DATABASE "{name}"')

    # -- child: seed, build, time ---------------------------------------------------

    def child(self, *, orders, customers, days, repeat, seed, reseed, as_json, **options):
        from django.db import connection

        if connection.vendor == "sqlite":
            name = Path(connection.settings_dict["NAME"]).resolve()
            if name == (Path(settings.BASE_DIR) / "db.sqlite3").resolve():
                raise CommandError("Refusing to benchmark on the demo database")
        call_command("migrate", verbosity=0)
        self.out("Database", f"{connection.vendor} {self.server_version()}")
        self.seed(orders, customers, days, seed, reseed)
        built = self.build()
        results = self.measure(repeat)
        self.print_table(results, built)
        if as_json:
            self.stdout.write(json.dumps({"built": built, "results": results}, indent=2))

    def out(self, label: str, value) -> None:
        self.stdout.write(f"{label + ':':<22} {value}")

    def server_version(self) -> str:
        from django.db import connection

        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute("SHOW server_version")
            else:
                cursor.execute("SELECT sqlite_version()")
            return cursor.fetchone()[0]

    def seed(self, orders: int, customers: int, days: int, seed: int, reseed: bool) -> None:
        import random

        from django.utils import timezone

        from bi_admin.calendar import table
        from bi_admin.execution import cache
        from shop.models import Customer, Order

        if Order.objects.count() == orders and not reseed:
            self.out("Data", f"{orders:,} orders already seeded (--reseed to redo)")
            return
        started = time.monotonic()
        call_command(
            "seed_demo", customers=customers, orders=0, flush=True, seed=seed, users=True,
            media=False, stdout=StringIO(),
        )  # fmt: skip
        rng = random.Random(seed)
        customer_ids = list(Customer.objects.values_list("pk", flat=True))
        now = timezone.now()
        batch = []
        for i in range(orders):
            age = int(days * (1 - rng.random() ** 0.7))
            created = now - timedelta(days=age, minutes=rng.randint(0, 1439))
            status = rng.choice(STATUSES)
            batch.append(
                Order(
                    customer_id=rng.choice(customer_ids),
                    status=status,
                    amount=Decimal(rng.randint(20, 5000) * 10_000),
                    created_at=created,
                    paid_at=created + timedelta(minutes=rng.randint(1, 600))
                    if status in ("paid", "refunded")
                    else None,
                )
            )
            if len(batch) == 20_000:
                Order.objects.bulk_create(batch)
                batch = []
                self.stdout.write(f"\r  seeded {i + 1:,} orders", ending="")
                self.stdout.flush()
        if batch:
            Order.objects.bulk_create(batch)
        self.stdout.write("")
        table.populate(*table.default_range())
        cache.bump_all()
        self.out("Seeded", f"{orders:,} orders, {customers:,} customers "
                           f"in {time.monotonic() - started:.1f} s")  # fmt: skip

    def build(self) -> dict:
        from bi_admin.scale import columnar, refresh
        from bi_admin.scale.preaggregations import all_preaggregations

        out = {}
        for preagg in all_preaggregations():
            started = time.monotonic()
            outcome = refresh.refresh(preagg, "rebuild")
            out[preagg.key] = {"rows": outcome["rows"], "seconds": time.monotonic() - started}
            self.out(f"Built {preagg.name}", f"{outcome['rows']:,} rows in "
                                            f"{time.monotonic() - started:.2f} s")  # fmt: skip
        backend = columnar.get("duckdb")
        if backend is not None and backend.available():
            started = time.monotonic()
            outcome = backend.sync()
            out["duckdb"] = {"rows": outcome["rows"], "seconds": time.monotonic() - started,
                             "bytes": backend.path().stat().st_size}  # fmt: skip
            self.out("Synced DuckDB", f"{outcome['rows']:,} rows in "
                                      f"{time.monotonic() - started:.2f} s")  # fmt: skip
        else:
            self.out("DuckDB", "not installed (pip install 'gozar[duckdb]'); skipped")
        return out

    def measure(self, repeat: int) -> list[dict]:
        from django.contrib.auth import get_user_model

        from bi_admin.engine import ReportError, run
        from bi_admin.execution.jobs import JobRequest

        User = get_user_model()
        results = []
        for title, raw in REPORTS:
            spec = {"semantic_model": ORDERS, **{k: v for k, v in raw.items() if k != "_user"}}
            request = None
            if "_user" in raw:
                request = JobRequest(User.objects.get(username=raw["_user"]), "en", "gregorian")
            row = {"report": title, "spec": spec, "engines": {}}
            reference = None
            for engine in ENGINES:
                times, served = [], None
                try:
                    for _ in range(repeat):
                        started = time.perf_counter()
                        result = run(spec, request, engine=engine)
                        times.append((time.perf_counter() - started) * 1000)
                    served = result.execution.get("pre_aggregation") or engine
                except ReportError as exc:
                    row["engines"][engine] = {"ms": None, "why": exc.code}
                    continue
                if reference is None:
                    reference = result.rows
                row["engines"][engine] = {
                    "ms": statistics.median(times),
                    "served_by": served,
                    "rows": len(result.rows),
                    "same": result.rows == reference,
                }
            results.append(row)
        return results

    def print_table(self, results: list[dict], built: dict) -> None:
        from django.db import connection

        self.stdout.write("")
        self.out("Machine", f"{cpu()} · {os.cpu_count()} threads · {memory()}")
        self.out(
            "Software",
            f"Python {platform.python_version()} · Django {django_version()} · "
            f"{connection.vendor} {self.server_version()} · DuckDB {duckdb_version()}",
        )  # noqa: E501 fmt: skip
        if connection.vendor == "sqlite":
            size = Path(connection.settings_dict["NAME"]).stat().st_size
            self.out("Database file", f"{size / 2**20:,.0f} MB")
        header = f"{'report':<52} {'ORM ms':>9} {'pre-agg ms':>11} {'DuckDB ms':>10}  same"
        self.stdout.write("\n" + header + "\n" + "-" * len(header))
        for row in results:
            cells = []
            for engine in ENGINES:
                found = row["engines"].get(engine) or {}
                cells.append(f"{found['ms']:.1f}" if found.get("ms") is not None else "—")
            same = all(e.get("same", True) for e in row["engines"].values())
            self.stdout.write(
                f"{row['report'][:52]:<52} {cells[0]:>9} {cells[1]:>11} {cells[2]:>10}  "
                f"{'yes' if same else 'NO'}"
            )
        self.stdout.write("— : the engine can't answer that report exactly (the ORM would).")


def cpu() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def memory() -> str:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                return f"{int(line.split()[1]) / 2**20:.0f} GB RAM"
    except OSError:
        pass
    return "unknown RAM"


def django_version() -> str:
    import django

    return django.get_version()


def duckdb_version() -> str:
    try:
        import duckdb
    except ImportError:
        return "not installed"
    return duckdb.__version__
