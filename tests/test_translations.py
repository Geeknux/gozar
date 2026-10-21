"""Per-language names and descriptions of saved reports, dashboards, widgets and
e-mail schedules: the stored form, the viewer's language with fallbacks, search,
exports, e-mails and the data migration."""

import datetime as dt
from urllib.parse import quote

import pytest
from conftest import API, SHOP_VIEW
from django.core import mail
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client
from django.utils import translation

from bi_admin import formatting, scheduling, translations
from bi_admin.models import Dashboard, DashboardWidget, SavedReport, Schedule, UserPreferences

pytestmark = pytest.mark.django_db

SAVED = f"{API}/saved-reports"
DASHBOARDS = f"{API}/dashboards"
BY_CITY = {
    "semantic_model": "order_metrics",
    "measures": ["revenue"],
    "dimensions": ["city"],
    "chart": "bar",
}
NAMES = {"en": "Revenue by city", "fa": "درآمد به تفکیک شهر"}
DESCRIPTIONS = {"en": "Paid revenue, last 3 months.", "fa": "درآمد پرداخت‌شده در سه ماه گذشته."}


def login(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def people(seeded, make_staff):
    users = {
        "en": make_staff(*SHOP_VIEW, username="english"),
        "fa": make_staff(*SHOP_VIEW, username="persian"),
    }
    UserPreferences.objects.create(user=users["fa"], language="fa")
    for user in users.values():
        user.email = f"{user.username}@example.com"
        user.save()
    return users


@pytest.fixture
def clients(people):
    return {lang: login(user) for lang, user in people.items()}


@pytest.fixture
def report(people):
    return SavedReport.objects.create(
        owner=people["en"],
        name_i18n=NAMES,
        description_i18n=DESCRIPTIONS,
        semantic_model="order_metrics",
        spec=BY_CITY,
        visibility="everyone",
    )


# --- the stored form and the fallbacks ------------------------------------------------


def test_pick_falls_back_to_the_default_language_then_the_plain_field_then_any():
    assert translations.pick(NAMES, "plain", "fa") == NAMES["fa"]
    assert translations.pick({"en": "English"}, "plain", "fa") == "English"
    assert translations.pick({}, "plain", "fa") == "plain"
    assert translations.pick({"fa": "فقط فارسی"}, "", "en") == "فقط فارسی"
    assert translations.pick(None, "", "en") == ""


def test_save_keeps_the_plain_field_and_the_default_language_in_step(people):
    report = SavedReport.objects.create(
        owner=people["en"], name="Plain", semantic_model="order_metrics", spec=BY_CITY
    )
    assert report.name_i18n == {"en": "Plain"}  # an old-style write fills the default
    report.name_i18n = {"en": "Plain", "fa": "ساده"}
    report.name = "Renamed"  # e.g. Django's admin, or a client that sends `name` only
    report.save(update_fields=["name"])
    report.refresh_from_db()
    assert report.name_i18n == {"en": "Renamed", "fa": "ساده"}  # update_fields widened

    only_persian = SavedReport.objects.create(
        owner=people["en"], name_i18n={"fa": "فقط فارسی"}, semantic_model="x", spec={}
    )
    # the plain field shows *something*, but English gets no fake translation
    assert only_persian.name == "فقط فارسی" and only_persian.name_i18n == {"fa": "فقط فارسی"}
    only_persian.save()
    assert only_persian.name_i18n == {"fa": "فقط فارسی"}
    assert only_persian.text("name", "en") == "فقط فارسی"


def test_default_language_setting_decides_what_the_plain_field_holds(people, settings):
    settings.GOZAR = {**settings.GOZAR, "DEFAULT_LANGUAGE": "fa"}
    report = SavedReport(owner=people["en"], semantic_model="order_metrics", spec=BY_CITY)
    translations.assign(report, "name", None, NAMES)
    assert report.name == NAMES["fa"]
    translations.assign(report, "name", "نام تازه", None)  # plain text: the default language
    assert report.name_i18n == {"en": NAMES["en"], "fa": "نام تازه"}


# --- API ----------------------------------------------------------------------------------


def test_api_returns_the_viewers_language_and_every_translation(clients, report):
    en = clients["en"].get(f"{SAVED}/{report.pk}").json()
    fa = clients["fa"].get(f"{SAVED}/{report.pk}").json()
    assert (en["name"], en["description"]) == (NAMES["en"], DESCRIPTIONS["en"])
    assert (fa["name"], fa["description"]) == (NAMES["fa"], DESCRIPTIONS["fa"])
    assert en["name_i18n"] == fa["name_i18n"] == NAMES
    assert en["description_i18n"] == DESCRIPTIONS


def test_missing_language_falls_back_to_the_default(clients, people):
    resp = clients["en"].post(
        SAVED,
        {
            "name_i18n": {"en": "Only English"},
            "spec": BY_CITY,
            "sharing": {"visibility": "everyone"},
        },
        "application/json",
    )
    assert resp.status_code == 201, resp.content
    report_id = resp.json()["id"]
    assert clients["fa"].get(f"{SAVED}/{report_id}").json()["name"] == "Only English"


def test_create_and_patch_translations(clients):
    resp = clients["fa"].post(
        SAVED,
        {
            "name_i18n": {"fa": f"  {NAMES['fa']}\u2069 ", "en": NAMES["en"], "de": ""},
            "spec": BY_CITY,
        },
        "application/json",
    )
    assert resp.status_code == 400  # an unknown language is refused, not dropped
    assert "name_i18n" in resp.json()["errors"]

    resp = clients["fa"].post(
        SAVED,
        {"name_i18n": {"fa": f" \u2068{NAMES['fa']}\u2069 ", "en": ""}, "spec": BY_CITY},
        "application/json",
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == NAMES["fa"]  # stripped, isolate marks removed
    assert data["name_i18n"] == {"fa": NAMES["fa"]}
    report = SavedReport.objects.get(pk=data["id"])
    assert report.name == NAMES["fa"]  # the only text there is

    resp = clients["fa"].patch(
        f"{SAVED}/{report.pk}",
        {"name_i18n": NAMES, "description_i18n": DESCRIPTIONS},
        "application/json",
    )
    assert resp.json()["name"] == NAMES["fa"]
    report.refresh_from_db()
    assert (report.name, report.description) == (NAMES["en"], DESCRIPTIONS["en"])

    # a plain name changes the default language only
    clients["fa"].patch(f"{SAVED}/{report.pk}", {"name": "Renamed"}, "application/json")
    report.refresh_from_db()
    assert report.name_i18n == {"en": "Renamed", "fa": NAMES["fa"]}

    # a name in no language at all is refused
    resp = clients["fa"].patch(
        f"{SAVED}/{report.pk}", {"name_i18n": {"fa": " "}}, "application/json"
    )
    assert resp.status_code == 400 and "name" in resp.json()["errors"]


def test_duplicate_translates_copy_of_into_each_language(clients, report):
    data = clients["en"].post(f"{SAVED}/{report.pk}/duplicate").json()
    assert data["name_i18n"]["en"] == f"Copy of {NAMES['en']}"
    assert data["name_i18n"]["fa"] != f"Copy of {NAMES['fa']}"
    assert NAMES["fa"] in data["name_i18n"]["fa"]
    assert data["description_i18n"] == DESCRIPTIONS


def test_search_matches_every_language(clients, report):
    def found(client, q):
        resp = client.get(SAVED, {"q": q})
        return {r["id"] for r in resp.json()["saved_reports"]}

    assert found(clients["en"], "تفکیک") == {report.pk}  # Persian text from an English UI
    assert found(clients["fa"], "by city") == {report.pk}
    assert found(clients["fa"], "سه ماه") == {report.pk}  # descriptions too
    assert found(clients["fa"], "nothing like it") == set()


def test_dashboards_and_widgets(clients, report):
    widgets = [
        {"saved_report": report.pk, "x": 0, "y": 0, "w": 6, "h": 4},
        {
            "title_i18n": {"en": "Orders by status", "fa": "سفارش‌ها بر اساس وضعیت"},
            "spec": {**BY_CITY, "chart": "pie"},
            "x": 6,
            "y": 0,
            "w": 6,
            "h": 4,
        },
    ]
    resp = clients["en"].post(
        DASHBOARDS,
        {
            "name_i18n": {"en": "Sales overview", "fa": "نمای کلی فروش"},
            "description_i18n": {"fa": "در یک نگاه"},
            "sharing": {"visibility": "everyone"},
            "widgets": widgets,
        },
        "application/json",
    )
    assert resp.status_code == 201, resp.content
    board_id = resp.json()["id"]
    fa = clients["fa"].get(f"{DASHBOARDS}/{board_id}").json()
    assert (fa["name"], fa["description"]) == ("نمای کلی فروش", "در یک نگاه")
    titles = [w["title"] for w in fa["widgets"]]
    assert titles == [NAMES["fa"], "سفارش‌ها بر اساس وضعیت"]  # a report widget: its name
    assert fa["widgets"][0]["title_i18n"] == {}  # no title of its own
    assert fa["widgets"][0]["saved_report"]["name"] == NAMES["fa"]
    en = clients["en"].get(f"{DASHBOARDS}/{board_id}").json()
    assert [w["title"] for w in en["widgets"]] == [NAMES["en"], "Orders by status"]
    assert en["description"] == "در یک نگاه"  # no English description: the one there is

    assert {b["id"] for b in clients["en"].get(DASHBOARDS, {"q": "نمای"}).json()["dashboards"]} == {
        board_id
    }
    resolved = clients["fa"].post(f"{DASHBOARDS}/{board_id}/resolve", {}, "application/json")
    assert [w["title"] for w in resolved.json()["widgets"]] == titles

    copy = clients["en"].post(f"{DASHBOARDS}/{board_id}/duplicate").json()
    copied = DashboardWidget.objects.filter(dashboard_id=copy["id"]).order_by("x")
    assert copied[0].title_i18n == NAMES  # the report's name, kept per language
    assert copied[1].title_i18n["fa"] == "سفارش‌ها بر اساس وضعیت"


def test_widget_title_errors_name_the_widget(clients):
    resp = clients["en"].post(
        DASHBOARDS,
        {"name": "Board", "widgets": [{"title_i18n": {"xx": "?"}, "spec": BY_CITY}]},
        "application/json",
    )
    assert resp.status_code == 400
    assert "widgets[0].title_i18n" in resp.json()["errors"]


def test_export_is_named_after_the_title(clients):
    resp = clients["fa"].post(
        f"{API}/reports/export?format=xlsx&title={quote(NAMES['fa'])}", BY_CITY, "application/json"
    )
    assert resp.status_code == 200
    disposition = resp["Content-Disposition"]
    assert disposition.startswith("attachment; filename*=utf-8''")
    assert quote("درآمد-به-تفکیک-شهر.xlsx") in disposition
    from io import BytesIO

    from openpyxl import load_workbook

    assert load_workbook(BytesIO(resp.content)).active.title == NAMES["fa"]
    plain = clients["en"].post(f"{API}/reports/export?format=csv", BY_CITY, "application/json")
    assert plain["Content-Disposition"] == 'attachment; filename="order_metrics.csv"'


# --- schedules and e-mails -------------------------------------------------------------------


@pytest.fixture
def schedule(people, report):
    s = Schedule.objects.create(
        owner=people["en"],
        saved_report=report,
        name_i18n={"en": "Weekly revenue", "fa": "درآمد هفتگی"},
        description_i18n={"en": "For the Monday meeting.", "fa": "برای جلسهٔ دوشنبه."},
        frequency="weekly",
        weekday=0,
        time=dt.time(8, 0),
        timezone="Asia/Tehran",
        attachment="xlsx",
    )
    s.recipients.set(people.values())
    return s


def test_schedule_api_carries_translations(clients, schedule):
    (en,) = clients["en"].get(f"{API}/schedules").json()["schedules"]
    assert (en["name"], en["description"]) == ("Weekly revenue", "For the Monday meeting.")
    assert en["name_i18n"] == {"en": "Weekly revenue", "fa": "درآمد هفتگی"}
    assert en["target"]["name"] == NAMES["en"]
    resp = clients["en"].patch(
        f"{API}/schedules/{schedule.pk}",
        {"name_i18n": {"fa": "گزارش هفتگی"}, "description_i18n": {}},
        "application/json",
    )
    assert resp.status_code == 200, resp.content
    schedule.refresh_from_db()
    assert schedule.name_i18n == {"fa": "گزارش هفتگی"} and schedule.description == ""
    assert resp.json()["name"] == "گزارش هفتگی"  # no English name: the Persian one


def test_each_recipient_gets_the_email_in_their_language(people, schedule):
    deliveries = scheduling.deliver(schedule)
    assert [d.status for d in deliveries] == ["sent", "sent"]
    by_user = {m.to[0].split("@")[0]: m for m in mail.outbox}
    en, fa = by_user["english"], by_user["persian"]
    assert en.subject.startswith("Weekly revenue")
    assert fa.subject.startswith("درآمد هفتگی ·")
    en_html, fa_html = en.alternatives[0][0], fa.alternatives[0][0]
    assert NAMES["en"] in en_html and DESCRIPTIONS["en"] in en_html
    assert "For the Monday meeting." in en_html
    assert NAMES["fa"] in fa_html and DESCRIPTIONS["fa"] in fa_html
    assert "برای جلسهٔ دوشنبه." in fa_html and NAMES["en"] not in fa_html
    assert '<h1 dir="auto">' in fa_html and 'lang="fa" dir="rtl"' in fa_html
    assert NAMES["fa"] in fa.body  # the plain-text part too
    (attachment,) = fa.attachments
    assert attachment[0] == "درآمد-به-تفکیک-شهر.xlsx"


def test_dashboard_email_uses_widget_titles_in_the_recipients_language(people, report):
    board = Dashboard.objects.create(
        owner=people["en"], name_i18n={"en": "Board", "fa": "داشبورد"}, visibility="everyone"
    )
    DashboardWidget.objects.create(
        dashboard=board,
        title_i18n={"en": "Cities", "fa": "شهرها"},
        spec=BY_CITY,
        chart="table",
    )
    DashboardWidget.objects.create(dashboard=board, saved_report=report, chart="bar", y=4)
    s = Schedule.objects.create(
        owner=people["en"],
        dashboard=board,
        frequency="daily",
        time=dt.time(8, 0),
        timezone="Asia/Tehran",
        attachment="",
    )
    _message, sections = scheduling.build_message(s, people["fa"])
    assert [sec.title for sec in sections] == ["شهرها", NAMES["fa"]]
    message, sections = scheduling.build_message(s, people["en"])
    assert [sec.title for sec in sections] == ["Cities", NAMES["en"]]
    assert message.subject.startswith("Board")  # no schedule name: the dashboard's


def test_isolate_only_marks_text_that_needs_it():
    fsi, pdi = "\u2068", "\u2069"
    assert formatting.isolate("Sales overview") == "Sales overview"
    assert formatting.isolate("نمای کلی فروش") == "نمای کلی فروش"
    assert formatting.isolate("Sales (Q4)") == f"{fsi}Sales (Q4){pdi}"  # neutral at an end
    assert formatting.isolate("گزارش Sales") == f"{fsi}گزارش Sales{pdi}"
    assert formatting.isolate("2026") == "2026" and formatting.isolate("") == ""
    # in a sentence of the same direction, it needs no isolation
    assert formatting.isolate("Sales (Q4)", "en") == "Sales (Q4)"
    assert formatting.isolate("Sales (Q4)", "fa") == f"{fsi}Sales (Q4){pdi}"
    assert formatting.isolate("گزارش Sales", "fa") == f"{fsi}گزارش Sales{pdi}"


def test_text_follows_the_active_language(report):
    with translation.override("fa"):
        assert report.text("name") == NAMES["fa"]
    with translation.override("en"):
        assert report.text("name") == NAMES["en"]


# --- the data migration ----------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_data_migration_keeps_existing_texts_as_the_default_language(django_user_model):
    before = [("bi_admin", "0010_translations")]
    after = [("bi_admin", "0011_translations_data")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old = executor.loader.project_state(before).apps
    owner = old.get_model("auth", "User").objects.create(username="legacy", is_staff=True)
    report = old.get_model("bi_admin", "SavedReport").objects.create(
        owner_id=owner.pk, name="Old report", description="Kept", semantic_model="m", spec={}
    )
    board = old.get_model("bi_admin", "Dashboard").objects.create(
        owner_id=owner.pk, name="Old board", name_i18n={"fa": "تابلوی قدیمی"}
    )
    widget = old.get_model("bi_admin", "DashboardWidget").objects.create(
        dashboard_id=board.pk, title="Old widget"
    )
    untitled = old.get_model("bi_admin", "DashboardWidget").objects.create(
        dashboard_id=board.pk, title=""
    )
    schedule = old.get_model("bi_admin", "Schedule").objects.create(
        owner_id=owner.pk,
        dashboard_id=board.pk,
        name="Old schedule",
        frequency="daily",
        time=dt.time(8),
        timezone="UTC",
    )

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    new = executor.loader.project_state(after).apps
    report = new.get_model("bi_admin", "SavedReport").objects.get(pk=report.pk)
    assert report.name_i18n == {"en": "Old report"}
    assert report.description_i18n == {"en": "Kept"}
    board = new.get_model("bi_admin", "Dashboard").objects.get(pk=board.pk)
    assert board.name_i18n == {"fa": "تابلوی قدیمی", "en": "Old board"}  # others kept
    assert board.description_i18n == {}  # nothing to keep
    widgets = new.get_model("bi_admin", "DashboardWidget").objects
    assert widgets.get(pk=widget.pk).title_i18n == {"en": "Old widget"}
    assert widgets.get(pk=untitled.pk).title_i18n == {}
    schedule = new.get_model("bi_admin", "Schedule").objects.get(pk=schedule.pk)
    assert schedule.name_i18n == {"en": "Old schedule"}

    MigrationExecutor(connection).migrate(executor.loader.graph.leaf_nodes())
