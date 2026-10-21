"""Check the SPA layout in a browser: wide tables fit their cards, pie legends are whole.

    uv run --with playwright python scripts/check_layout.py [--shots DIR]

Runs the example project on a copy of the demo database, as scripts/screenshots.py
does (port 8100 by default, so a server on 8000 keeps running), and drives headless
Chromium in English and Persian at several widths:

- at 1280 px and wider, no table on the System page, the audit log, the delivery log
  or the question log scrolls sideways inside its card;
- at every width (phones too) the System page's tables list fits its card;
- every pie or donut chart on the home dashboard lists its slices in a legend whose
  names are shown whole (no ellipsis, no clipping).

It prints each problem and exits with status 1 if there is any. --shots saves a
screenshot of every page it checks, to look at.
"""

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright
from screenshots import ROOT, demo_server, settle

WIDE, PHONE = [1280, 1440, 1920], [390]

# Tables that scroll sideways inside their .table-wrap (more than a pixel of rounding).
OVERFLOWING = """(sel) => [...document.querySelectorAll(sel)]
  .filter((w) => w.offsetParent !== null && w.scrollWidth > w.clientWidth + 1)
  .map((w) => {
    const card = w.closest('section, .card');
    const title = card && card.querySelector('h2, h3');
    const name = title ? title.textContent.trim() : '(table)';
    return `${name}: ${w.scrollWidth}px in ${w.clientWidth}px`;
  })"""

# Pie legends: how many there are, and the names that don't fit their box.
PIE_LEGENDS = """() => {
  const legends = [...document.querySelectorAll('.chart-pie .chart-legend')];
  const cut = (n) => n.scrollWidth > n.clientWidth + 1
    || getComputedStyle(n).textOverflow === 'ellipsis';
  const clipped = legends.flatMap((l) => [...l.querySelectorAll('.chart-legend-name')])
    .filter(cut).map((n) => n.textContent);
  const empty = legends.filter((l) => !l.querySelector('li')).length;
  return {count: legends.length, clipped, empty};
}"""

PREFERENCES = """async (body) => {
  const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
  await fetch('/gozar/api/preferences', {method: 'PATCH', body: JSON.stringify(body),
    headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf}});
}"""

PAGES = [
    # (user, path, element to wait for, table wrappers to check, widths)
    ("admin", "/system", ".sys-tables tbody tr", ".system-page .table-wrap", WIDE),
    ("admin", "/system", ".sys-tables tbody tr", ".sys-tables-card .table-wrap", PHONE),
    ("admin", "/audit", ".audit-table tbody tr", ".table-wrap", WIDE),
    ("admin", "/audit/deliveries", ".content .table-wrap, .content .empty", ".table-wrap", WIDE),
    ("admin", "/questions", ".audit-table tbody tr, .content .empty", ".table-wrap", WIDE),
]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--shots", type=Path, help="save a screenshot of every page checked here")
    args = parser.parse_args()
    source = ROOT / "example_project" / "db.sqlite3"
    if not source.exists():
        sys.exit(f"{source} not found: seed the demo first (README, Development).")
    if args.shots:
        args.shots.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="gozar-layout-"))
    try:
        with demo_server(source, tmp, args.port) as (base, sessions, _, _):
            problems = check(base, sessions, args.shots)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for problem in problems:
        print(problem)
    if problems:
        sys.exit(f"{len(problems)} layout problem(s)")
    print("layout OK")


class Session:
    """One browser context per user, in one language, resized as needed."""

    def __init__(self, browser, base, sessions, lang, shots):
        self.browser, self.base, self.sessions = browser, base, sessions
        self.lang, self.shots = lang, shots
        self.pages = {}

    def page(self, user, width):
        if user not in self.pages:
            context = self.browser.new_context(
                viewport={"width": width, "height": 900}, timezone_id="Asia/Tehran"
            )
            context.add_cookies(
                [{"name": "sessionid", "value": self.sessions[user], "url": self.base}]
            )
            page = self.pages[user] = context.new_page()
            # the preferences land in the copy of the database, like everything else
            page.goto(f"{self.base}/gozar/login")
            calendar = "jalali" if self.lang == "fa" else "gregorian"
            page.evaluate(PREFERENCES, {"language": self.lang, "calendar": calendar})
        self.pages[user].set_viewport_size({"width": width, "height": 900})
        return self.pages[user]

    def visit(self, user, path, wait, width):
        page = self.page(user, width)
        page.goto(f"{self.base}/gozar{path}")
        page.wait_for_selector(wait, timeout=30_000)
        settle(page, 900)
        if self.shots:
            name = path.strip("/").replace("/", "-") or "home"
            shot = self.shots / f"{self.lang}-{name}-{user}-{width}.png"
            page.screenshot(path=shot, full_page=True)
        return page

    def close(self):
        for page in self.pages.values():
            page.context.close()


def check(base, sessions, shots):
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("GOZAR_CHROMIUM") or None)
        for lang in ["en", "fa"]:
            session = Session(browser, base, sessions, lang, shots)
            for user, path, wait, wraps, widths in PAGES:
                for width in widths:
                    page = session.visit(user, path, wait, width)
                    for overflow in page.evaluate(OVERFLOWING, wraps):
                        problems.append(f"[{lang} {width}px {path}] scrolls sideways: {overflow}")
            for width in WIDE + PHONE:
                page = session.visit("manager", "/", ".chart-pie .chart-legend li", width)
                legends = page.evaluate(PIE_LEGENDS)
                where = f"[{lang} {width}px dashboard]"
                if legends["empty"]:
                    problems.append(f"{where} {legends['empty']} pie legend(s) without slices")
                for name in legends["clipped"]:
                    problems.append(f"{where} pie legend name cut off: {name!r}")
            session.close()
        browser.close()
    return problems


if __name__ == "__main__":
    main()
