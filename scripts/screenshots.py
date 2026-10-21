"""Capture the README screenshots (docs/screenshots) from the demo database.

    uv run --with playwright python scripts/screenshots.py [--only NAME ...]

The demo must be seeded first (README, Development). The script copies
example_project/db.sqlite3 to a temporary directory, starts the example project
on that copy (port 8100 by default, so a server on 8000 keeps running), signs the
demo users in by creating their sessions in the copy — no passwords — and drives
headless Chromium at 1440x900 with a device scale factor of 2. Every write
(sessions, preferences, audit log, cache) lands in the copy, which is deleted at
the end. Chromium is Playwright's own (`playwright install chromium`), or the
binary in $GOZAR_CHROMIUM.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
MANAGE = ROOT / "example_project" / "manage.py"
WIDTH, HEIGHT, SCALE = 1440, 900, 2
USERS = ["admin", "manager", "tehran"]

SESSIONS = """
import json
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.contrib.sessions.backends.db import SessionStore
out = {}
for user in get_user_model().objects.filter(username__in=%r):
    session = SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session.create()
    out[user.username] = session.session_key
print("SESSIONS=" + json.dumps(out))
"""

# Products import file for the dry run: two updates (by ID), two new rows, one error.
IMPORT_CSV = """id,name,category,price,is_active
37,Smartphone,electronics,26500000,true
40,Novel,books,390000,true
,Desk lamp,home,1850000,true
,Green tea,grocery,240000,true
,Phone case,toys,150000,true
"""


def spec_url(spec):
    return "/reports?spec=" + urllib.parse.quote(json.dumps(spec))


CITY_REVENUE = {
    "semantic_model": "order_metrics",
    "measures": ["revenue"],
    "dimensions": ["city"],
    "relative_period": "last_3_months",
    "compare_to": "previous_period",
    "sort": [{"field": "revenue", "direction": "desc"}],
}
PIVOT = {
    "semantic_model": "order_metrics",
    "measures": ["orders"],
    "dimensions": ["city", "status"],
    "relative_period": "past_12_months",
    "chart": "pivot",
    "sort": [{"field": "orders", "direction": "desc"}],
}


class Capture:
    def __init__(self, browser, base, sessions, refresh):
        self.browser, self.base, self.sessions, self.refresh = browser, base, sessions, refresh
        self.pages = {}
        self.last = None

    def page(self, user):
        if user not in self.pages:
            context = self.browser.new_context(
                viewport={"width": WIDTH, "height": HEIGHT},
                device_scale_factor=SCALE,
                color_scheme="light",
                locale="en-US",
                timezone_id="Asia/Tehran",
            )
            context.add_cookies(
                [{"name": "sessionid", "value": self.sessions[user], "url": self.base}]
            )
            page = context.new_page()
            page.on("pageerror", lambda e: print(f"  page error ({user}): {e}", file=sys.stderr))
            self.pages[user] = page
        return self.pages[user]

    def open(self, user, path, wait=None):
        page = self.page(user)
        self.last = page
        page.goto(self.base + "/gozar" + path)
        page.wait_for_selector(wait or ".sidebar")
        settle(page)
        return page

    def api(self, user, path, method="GET", body=None):
        return self.page(user).evaluate(
            """async ([path, method, body]) => {
            const csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || '';
            const r = await fetch('/gozar/api' + path, {method,
              body: body ? JSON.stringify(body) : undefined,
              headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf}});
            return r.json();
        }""",
            [path, method, body],
        )


def settle(page, ms=1800):
    """Wait for requests, charts' entry animation and web fonts, then hide the caret."""
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(ms)
    page.evaluate(
        """() => { document.activeElement && document.activeElement.blur();
        if (!document.getElementById('shot-style')) { const s = document.createElement('style');
        s.id = 'shot-style'; s.textContent = '* { caret-color: transparent !important; }';
        document.head.appendChild(s); } }"""
    )


def scroll_to(page, selector, offset=16):
    page.evaluate(
        "([sel, off]) => window.scrollTo(0, document.querySelector(sel).getBoundingClientRect().top"
        " + window.scrollY - off)",
        [selector, offset],
    )
    page.wait_for_timeout(300)


def png(page, locator=None):
    data = (locator or page).screenshot(animations="disabled")
    return Image.open(io.BytesIO(data)).convert("RGB")


# --- shots: each returns a full-resolution (2x) image -------------------------------------


def dashboard(c):
    return png(c.open("manager", "/", ".kpis"))


def report_builder(c):
    return png(c.open("manager", spec_url({**CITY_REVENUE, "chart": "bar"}), ".results canvas"))


def pivot(c):
    return png(c.open("manager", spec_url(PIVOT), ".results table"))


def drilldown(c):
    page = c.open("manager", spec_url(CITY_REVENUE), ".results .cell-link")
    page.locator(".results .cell-link").first.click()
    page.wait_for_selector(".overlay table tbody tr")
    settle(page, 400)
    return png(page)


def ask(c):
    page = c.open("manager", "/reports", ".ask-form input")
    for n, question in enumerate(["monthly revenue this year", "compared to last year"], 1):
        page.fill(".ask-form input", question)
        page.keyboard.press("Enter")
        page.wait_for_function(
            "n => document.querySelectorAll('.ask-thread .ask-answer').length >= n", arg=n
        )
        page.wait_for_selector(".results canvas")
        settle(page)
    return png(page)


def orders_list(c):
    year = time.localtime().tm_year
    page = c.open("manager", f"/shop/order?status=paid&dh_year={year}", ".date-hierarchy")
    return png(page)


def order_detail(c):
    page = c.open("manager", f"/shop/order/{c.order}", ".fieldset")
    return png(page)


def product_images(c):
    page = c.open("admin", f"/shop/product/{c.product}", ".fieldset")
    page.wait_for_selector(".content img")
    page.wait_for_function("[...document.querySelectorAll('.content img')].every(i => i.complete)")
    scroll_to(page, ".stacked-rows", 330)
    return png(page)


def permission_tree(c):
    # A group with permissions (the demo users get theirs from groups); same tree as users.
    page = c.open("admin", f"/auth/group/{c.group}", ".perm-apps")
    page.locator(".perm-apps > li", has_text="Shop").locator("button").first.click()
    scroll_to(page, ".perm-tree", 120)
    settle(page, 300)
    return png(page)


def import_preview(c):
    page = c.open("admin", "/shop/product", "table")
    page.get_by_role("button", name="Import", exact=True).click()
    page.set_input_files(
        ".import-dialog input[type=file]",
        files=[{"name": "products.csv", "mimeType": "text/csv", "buffer": IMPORT_CSV.encode()}],
    )
    page.get_by_role("button", name="Check file").click()
    page.wait_for_selector(".import-rows tbody tr")
    settle(page, 400)
    return png(page)


def global_search(c):
    return png(c.open("manager", "/search?q=" + urllib.parse.quote("Ahmadi"), ".content a"))


def system_database(c):
    page = c.open("admin", "/system", ".sys-columns canvas")
    page.get_by_role("button", name="Count all rows").click()  # exact counts (read-only)
    page.wait_for_function(
        "document.querySelectorAll('.sys-columns canvas').length >= 2", timeout=60_000
    )
    settle(page)
    scroll_to(page, ".sys-columns:has(canvas)")  # the Scale section has a .sys-columns too
    return png(page)


def system_scale(c):
    c.refresh()  # fresh, not "Refresh due", however long the earlier shots took
    page = c.open("admin", "/system", ".sys-scale .sys-preaggs tbody tr")
    scroll_to(page, ".sys-scale")
    return png(page)


def email_schedule(c):
    page = c.open("manager", "/schedules", "table")
    page.locator("table tbody tr").first.get_by_role("button", name="Edit").click()
    page.wait_for_selector(".overlay .dialog")
    settle(page, 400)
    return png(page)


def dashboard_persian(c):
    c.open("manager", "/", ".kpis")
    c.api("manager", "/preferences", "PATCH", {"language": "fa", "calendar": "jalali"})
    try:
        return png(c.open("manager", "/", ".kpis"))
    finally:
        c.api("manager", "/preferences", "PATCH", {"language": "en", "calendar": "gregorian"})


def row_level_security(c):
    spec = {**CITY_REVENUE, "compare_to": None, "measures": ["revenue", "orders"], "chart": "bar"}
    spec = {k: v for k, v in spec.items() if v is not None}
    panels = []
    for user in ["manager", "tehran"]:
        page = c.open(user, spec_url(spec), ".results canvas")
        panels.append((user, png(page, page.locator(".results"))))
    return side_by_side(panels)


def side_by_side(panels, gap=48, label_height=96):
    font = label_font(44)
    width = sum(img.width for _, img in panels) + gap * (len(panels) + 1)
    height = max(img.height for _, img in panels) + label_height + gap
    out = Image.new("RGB", (width, height), "#f6f6fa")
    draw = ImageDraw.Draw(out)
    x = gap
    for user, img in panels:
        draw.text((x, gap // 2 + 8), f"Signed in as {user}", fill="#1d1b2e", font=font)
        out.paste(img, (x, label_height + gap // 2))
        x += img.width + gap
    return out


def label_font(size):
    for name in [
        "DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "Arial Bold.ttf",
    ]:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


SHOTS = {
    "dashboard-sales-overview": dashboard,
    "report-builder-comparison": report_builder,
    "report-pivot-table": pivot,
    "report-drilldown": drilldown,
    "ask-follow-up": ask,
    "orders-list": orders_list,
    "order-detail-inline": order_detail,
    "product-image-inline": product_images,
    "user-permission-tree": permission_tree,
    "import-dry-run": import_preview,
    "global-search": global_search,
    "system-database": system_database,
    "system-scale": system_scale,
    "email-schedule": email_schedule,
    "dashboard-persian-jalali": dashboard_persian,
    "row-level-security": row_level_security,
}


def save(img, path, width):
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    img.save(path, "WEBP", quality=92, method=6)
    return img.size


def up(base):
    try:
        urllib.request.urlopen(base + "/gozar/login", timeout=1)
        return True
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--only", nargs="+", choices=SHOTS, help="capture only these shots")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    parser.add_argument(
        "--width", type=int, default=WIDTH, help="output width in pixels (default 1440)"
    )
    parser.add_argument(
        "--debug-dir", type=Path, default=Path(tempfile.gettempdir()), help="where failed shots go"
    )
    args = parser.parse_args()

    source = ROOT / "example_project" / "db.sqlite3"
    if not source.exists():
        sys.exit(f"{source} not found: seed the demo first (README, Development).")
    args.out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="gozar-shots-"))
    try:
        capture_all(args, source, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def capture_all(args, source, tmp):
    with demo_server(source, tmp, args.port) as (base, sessions, refresh, ids):
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("GOZAR_CHROMIUM") or None)
            capture = Capture(browser, base, sessions, refresh)
            capture.group, capture.order, capture.product = ids
            failed = []
            for name in args.only or SHOTS:
                try:
                    img = SHOTS[name](capture)
                except Exception as e:  # keep going; show what the page looked like
                    failed.append(name)
                    debug = args.debug_dir / f"gozar-shot-{name}.png"
                    if capture.last:
                        capture.last.screenshot(path=debug)
                    reason = f"{type(e).__name__}: {str(e).splitlines()[0]}"
                    print(f"{name}: FAILED ({reason}), see {debug}")
                    continue
                path = args.out / f"{name}.webp"
                size = save(img, path, args.width)
                print(f"{path.name}  {size[0]}x{size[1]}  {path.stat().st_size // 1024} KB")
            browser.close()
            if failed:
                sys.exit(f"failed: {', '.join(failed)}")


@contextlib.contextmanager
def demo_server(source, tmp, port):
    """Run the example project on a copy of ``source`` in ``tmp``; yields the base URL,
    the demo users' session keys, a function that refreshes the summary tables, and
    the ids of the Shop managers group, the largest order and the most pictured product."""
    db = tmp / "db.sqlite3"
    with sqlite3.connect(source) as src, sqlite3.connect(db) as dst:
        src.backup(dst)  # a consistent copy, even while another server uses the file
    env = {
        **os.environ,
        "GOZAR_SQLITE_PATH": str(db),
        "GOZAR_CACHE_DIR": str(tmp / "cache"),
        "GOZAR_EMAIL_DIR": str(tmp / "mail"),
        "GOZAR_DUCKDB_PATH": str(tmp / "gozar.duckdb"),
        "GOZAR_DEFAULT_LANGUAGE": "en",
        "GOZAR_DEFAULT_CALENDAR": "gregorian",
    }
    # The optional DuckDB engine (`uv sync --extra duckdb`) shows on the Scale section.
    if importlib.util.find_spec("duckdb"):
        env["GOZAR_DUCKDB"] = "1"
    else:
        env.pop("GOZAR_DUCKDB", None)

    def manage(*command, **kwargs):
        return subprocess.run(
            [sys.executable, str(MANAGE), *command], env=env, check=True, **kwargs
        )

    def refresh():
        manage("gozar_preaggregate", stdout=subprocess.DEVNULL)
        if env.get("GOZAR_DUCKDB"):
            manage("gozar_columnar_sync", stdout=subprocess.DEVNULL)

    refresh()
    out = manage("shell", "-c", SESSIONS % USERS, capture_output=True, text=True).stdout
    sessions = json.loads(
        next(line for line in out.splitlines() if line.startswith("SESSIONS="))[9:]
    )
    with sqlite3.connect(db) as conn:
        group = conn.execute("select id from auth_group where name = 'Shop managers'").fetchone()[0]
        # The order with the most items and the product with the most pictures.
        order = conn.execute(
            "select order_id from shop_orderitem group by order_id"
            " order by count(*) desc, order_id desc"
        ).fetchone()[0]
        product = conn.execute(
            "select product_id from shop_productimage group by product_id"
            " order by count(*) desc, product_id"
        ).fetchone()[0]

    base = f"http://127.0.0.1:{port}"
    if up(base):
        sys.exit(f"port {port} is in use; pick another with --port")
    server = subprocess.Popen(
        [sys.executable, str(MANAGE), "runserver", f"127.0.0.1:{port}", "--noreload"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if up(base):
                break
            time.sleep(0.2)
        else:
            sys.exit(f"the example project did not start on port {port}")
        yield base, sessions, refresh, (group, order, product)
    finally:
        server.terminate()
        server.wait()


if __name__ == "__main__":
    main()
