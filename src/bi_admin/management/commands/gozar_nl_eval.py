"""Score the NL golden set against a provider: ``manage.py gozar_nl_eval``.

By default it uses the configured provider (``GOZAR["NL"]["PROVIDER"]``, i.e. a
real LLM when ``GOZAR_NL_BASE_URL``/``GOZAR_NL_MODEL`` are set). Questions run as
``--user`` (a superuser by default) with the NL cache bypassed, against today's
date pinned to ``--today`` (the date the golden set was written for). Expected
category values are those of the English demo data (``seed_demo``).
"""

import datetime as dt
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory
from django.utils import translation

from bi_admin.nl import evaluation
from bi_admin.nl.providers import ProviderError, get_provider


def default_golden() -> Path | None:
    for base in [Path.cwd(), *Path.cwd().parents]:
        candidate = base / "tests" / "nl_golden"
        if candidate.is_dir():
            return candidate
    return None


class Command(BaseCommand):
    help = "Evaluate the natural-language interface against the golden set."

    def add_arguments(self, parser):
        parser.add_argument("--golden", help="golden JSON file or directory (tests/nl_golden)")
        parser.add_argument(
            "--provider", help="openai, heuristic, stub or a dotted path (default: configured)"
        )
        parser.add_argument("--user", help="username to ask as (default: the first superuser)")
        parser.add_argument("--today", default=evaluation.DEFAULT_TODAY.isoformat())
        parser.add_argument("--language", default="en", choices=["en", "fa"])
        parser.add_argument("--only", help="comma-separated case ids or file names (fa,en,…)")
        parser.add_argument("--limit", type=int, help="evaluate at most this many cases")
        parser.add_argument("--json", dest="json_path", help="write the full report here")
        parser.add_argument(
            "--fail-under", type=float, help="exit 1 when the semantic match rate is lower"
        )
        parser.add_argument("--verbose-failures", action="store_true")

    def handle(self, *args, **options):
        golden = Path(options["golden"]) if options["golden"] else default_golden()
        if golden is None or not golden.exists():
            raise CommandError("Golden set not found; pass --golden PATH")
        cases = evaluation.load(golden)
        if options["only"]:
            wanted = {w.strip() for w in options["only"].split(",")}
            cases = [c for c in cases if c.id in wanted or wanted & set(c.tags)]
        if options["limit"]:
            cases = cases[: options["limit"]]
        try:
            provider = get_provider(options["provider"])
        except ProviderError as exc:
            raise CommandError(str(exc)) from None
        user = self._user(options["user"])
        request = RequestFactory().get("/")
        request.user = user
        today = dt.date.fromisoformat(options["today"])

        described = provider.describe()
        self.stdout.write(
            f"Evaluating {len(cases)} cases with {described.get('name')} "
            f"{described.get('model') or ''} as {user.get_username()}…"
        )
        with translation.override(options["language"]):
            report = evaluation.evaluate(cases, request, provider, today)
        for name, value in report.metrics.items():
            self.stdout.write(f"  {name:22} {value}")
        failures = report.failures()
        if failures:
            self.stdout.write(f"\n{len(failures)} case(s) without a semantic match:")
            for o in failures:
                line = f"  {o.case.id:12} {o.status:13} {o.case.question}"
                self.stdout.write(line)
                if options["verbose_failures"]:
                    self.stdout.write(
                        f"      expected {json.dumps(o.case.expected, ensure_ascii=False)}"
                    )
                    self.stdout.write(f"      got      {json.dumps(o.spec, ensure_ascii=False)}")
                    if o.clarification or o.error:
                        self.stdout.write(f"      note     {o.clarification or o.error}")
        if options["json_path"]:
            Path(options["json_path"]).write_text(
                json.dumps(report.as_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
            )
            self.stdout.write(f"\nReport written to {options['json_path']}")
        threshold = options["fail_under"]
        if threshold is not None and report.metrics["semantic_match_rate"] < threshold:
            raise CommandError(
                f"Semantic match rate {report.metrics['semantic_match_rate']} < {threshold}"
            )

    def _user(self, username):
        User = get_user_model()
        if username:
            try:
                return User.objects.get(username=username)
            except User.DoesNotExist:
                raise CommandError(f"No user {username!r}") from None
        user = User.objects.filter(is_superuser=True, is_active=True).order_by("pk").first()
        if user is None:
            raise CommandError("No superuser found; pass --user")
        return user
