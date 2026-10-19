"""Per-user preferences (language + calendar), defaults, and language switching."""

import pytest
from django.test import Client

from bi_admin.models import ChangeLog, UserPreferences
from shop.models import City

API = "/gozar/api"
PREFS = f"{API}/preferences"

pytestmark = pytest.mark.django_db


def patch(client, body):
    return client.patch(PREFS, body, "application/json")


# --- the preferences API -------------------------------------------------------------


def test_preferences_require_a_staff_session(client, django_user_model):
    assert client.get(PREFS).status_code == 401
    assert patch(client, {"language": "fa"}).status_code == 401
    client.force_login(django_user_model.objects.create_user("joe", password="pw-123456!"))
    assert client.get(PREFS).status_code == 401
    assert patch(client, {"language": "fa"}).status_code == 401
    assert not UserPreferences.objects.exists()


def test_defaults_without_a_stored_row(admin_client):
    body = admin_client.get(PREFS).json()
    assert (body["language"], body["calendar"]) == ("en", "gregorian")
    assert body["stored"] == {"language": "", "calendar": ""}
    assert body["defaults"] == {
        "language": "en",
        "calendar": "gregorian",
        "timezone": "Asia/Tehran",
    }
    assert body["timezone"] == "Asia/Tehran"
    assert [lang["value"] for lang in body["available"]["languages"]] == ["en", "fa"]
    assert body["available"]["languages"][1] == {"value": "fa", "label": "فارسی", "dir": "rtl"}
    assert [c["value"] for c in body["available"]["calendars"]] == ["gregorian", "jalali"]
    assert not UserPreferences.objects.exists()  # reading never creates a row


def test_project_defaults_come_from_settings(admin_client, settings):
    settings.GOZAR = {"DEFAULT_LANGUAGE": "fa", "DEFAULT_CALENDAR": "jalali"}
    body = admin_client.get(PREFS).json()
    assert (body["language"], body["calendar"]) == ("fa", "jalali")
    settings.GOZAR = {"DEFAULT_LANGUAGE": "de", "DEFAULT_CALENDAR": "lunar"}
    body = admin_client.get(PREFS).json()
    assert (body["language"], body["calendar"]) == ("en", "gregorian")  # unsupported → fallback


def test_patch_stores_and_merges(admin_client, admin_user):
    body = patch(admin_client, {"language": "fa", "options": {"page_size": 50}}).json()
    assert (body["language"], body["calendar"]) == ("fa", "gregorian")
    body = patch(admin_client, {"calendar": "jalali", "options": {"theme": "dark"}}).json()
    assert (body["language"], body["calendar"]) == ("fa", "jalali")
    assert body["options"] == {"page_size": 50, "theme": "dark"}
    row = UserPreferences.objects.get(user=admin_user)
    assert (row.language, row.calendar) == ("fa", "jalali")
    # null / "" resets a field to the project default
    body = patch(admin_client, {"language": None, "calendar": ""}).json()
    assert (body["language"], body["calendar"]) == ("en", "gregorian")
    assert body["stored"] == {"language": "", "calendar": ""}


def test_patch_validation(admin_client):
    resp = patch(admin_client, {"language": "de", "calendar": "lunar", "colour": "red"})
    assert resp.status_code == 400
    errors = resp.json()["errors"]
    assert set(errors) == {"language", "calendar", "colour"}
    assert patch(admin_client, {"options": [1]}).status_code == 400
    assert admin_client.patch(PREFS, "not json", "application/json").status_code == 400
    assert admin_client.patch(PREFS, "[1]", "application/json").status_code == 400
    assert not UserPreferences.objects.exists()


def test_preferences_are_per_user(client, make_staff):
    alice, bob = make_staff(username="alice"), make_staff(username="bob")
    client.force_login(alice)
    patch(client, {"language": "fa", "calendar": "jalali"})
    client.force_login(bob)
    assert client.get(PREFS).json()["language"] == "en"
    assert UserPreferences.objects.get(user=alice).language == "fa"


def test_staff_without_model_permissions_can_still_set_preferences(client, make_staff):
    client.force_login(make_staff())  # no model permissions at all
    assert patch(client, {"calendar": "jalali"}).status_code == 200


def test_patch_requires_csrf(admin_user):
    c = Client(enforce_csrf_checks=True)
    c.force_login(admin_user)
    assert patch(c, {"language": "fa"}).status_code in (401, 403)


def test_auth_me_returns_preferences(admin_client):
    anon = Client().get(f"{API}/auth/me").json()
    assert anon["authenticated"] is False
    assert (anon["preferences"]["language"], anon["preferences"]["calendar"]) == (
        "en",
        "gregorian",
    )
    patch(admin_client, {"language": "fa", "calendar": "jalali"})
    me = admin_client.get(f"{API}/auth/me").json()
    assert me["authenticated"] is True
    assert (me["preferences"]["language"], me["preferences"]["calendar"]) == ("fa", "jalali")


def test_login_response_uses_the_users_language(client, admin_user):
    UserPreferences.objects.create(user=admin_user, language="fa")
    client.get(f"{API}/auth/csrf")
    bad = client.post(
        f"{API}/auth/login", {"username": "root", "password": "nope"}, "application/json"
    )
    assert bad.json()["detail"].startswith("Incorrect username")  # anonymous: English
    ok = client.post(
        f"{API}/auth/login", {"username": "root", "password": "s3cret-pass!"}, "application/json"
    )
    assert ok.json()["preferences"]["language"] == "fa"
    assert ok["Content-Language"] == "fa"


# --- language switching ------------------------------------------------------------


def test_api_labels_follow_the_users_language(admin_client, seeded):
    meta = admin_client.get(f"{API}/meta/models/shop/order").json()
    assert (meta["verbose_name"], meta["app_verbose_name"]) == ("order", "Shop")
    actions = {a["name"]: a["label"] for a in meta["list"]["actions"]}
    assert actions == {
        "delete_selected": "Delete selected",
        "mark_cancelled": "Cancel selected orders",
    }

    resp = patch(admin_client, {"language": "fa"})
    assert resp["Content-Language"] == "fa"  # the PATCH response is already Persian
    meta = admin_client.get(f"{API}/meta/models/shop/order").json()
    assert (meta["verbose_name"], meta["app_verbose_name"]) == ("سفارش", "فروشگاه")
    cols = {c["name"]: c["label"] for c in meta["list"]["columns"]}
    assert cols["customer__city__name"] == "مشتری / شهر / نام"
    filters = {f["name"]: f for f in meta["list"]["filters"]}
    assert filters["status"]["choices"][0] == {"value": "pending", "label": "در انتظار پرداخت"}
    actions = {a["name"]: a["label"] for a in meta["list"]["actions"]}
    assert actions["mark_cancelled"] == "لغو سفارش‌های انتخاب‌شده"
    index = admin_client.get(f"{API}/meta/models").json()["apps"]
    assert {a["verbose_name"] for a in index} >= {"فروشگاه"}

    patch(admin_client, {"language": "en"})
    meta = admin_client.get(f"{API}/meta/models/shop/order").json()
    assert meta["verbose_name"] == "order"


def test_errors_follow_the_users_language(admin_client):
    url = f"{API}/crud/shop/city/"
    assert (
        admin_client.get(f"{API}/crud/shop/nope/").json()["detail"] == "Unknown model 'shop.nope'"
    )
    resp = admin_client.post(url, {"province": "x", "bogus": 1}, "application/json")
    assert resp.json()["errors"]["bogus"] == ["This field cannot be edited."]
    resp = admin_client.post(url, {"province": "x"}, "application/json")
    assert resp.json()["errors"]["name"] == ["This field is required."]  # Django's own catalog

    patch(admin_client, {"language": "fa"})
    assert (
        admin_client.get(f"{API}/crud/shop/nope/").json()["detail"] == "مدل ناشناخته: 'shop.nope'"
    )
    resp = admin_client.post(url, {"province": "x", "bogus": 1}, "application/json")
    assert resp.json()["detail"] == "فیلدهای ناشناخته یا فقط‌خواندنی"
    assert resp.json()["errors"]["bogus"] == ["این فیلد قابل ویرایش نیست."]
    resp = admin_client.post(url, {"province": "x"}, "application/json")
    assert resp.json()["errors"]["name"] == ["این فیلد لازم است."]


def test_history_action_labels_are_translated(admin_client):
    resp = admin_client.post(
        f"{API}/crud/shop/city/", {"name": "Yazd", "province": "Yazd"}, "application/json"
    )
    pk = resp.json()["pk"]
    entries = admin_client.get(f"{API}/crud/shop/city/{pk}/history").json()["entries"]
    assert entries[0]["action_display"] == "Addition"
    patch(admin_client, {"language": "fa"})
    entries = admin_client.get(f"{API}/crud/shop/city/{pk}/history").json()["entries"]
    assert entries[0]["action_display"] == "افزودن"
    assert ChangeLog.objects.count() == 1 and City.objects.filter(name="Yazd").exists()


def test_anonymous_requests_follow_the_browser(client):
    me = client.get(f"{API}/auth/me", HTTP_ACCEPT_LANGUAGE="fa-IR,fa;q=0.9,en;q=0.5")
    assert me.json()["preferences"]["language"] == "fa" and me["Content-Language"] == "fa"
    me = client.get(f"{API}/auth/me", HTTP_ACCEPT_LANGUAGE="de-DE")
    assert me.json()["preferences"]["language"] == "en"
    bad = client.post(
        f"{API}/auth/login",
        {"username": "x", "password": "y"},
        "application/json",
        HTTP_ACCEPT_LANGUAGE="fa",
    )
    assert bad.json()["detail"].startswith("نام کاربری")


def test_stored_language_beats_the_browser(admin_client, admin_user):
    UserPreferences.objects.create(user=admin_user, language="en")
    me = admin_client.get(f"{API}/auth/me", HTTP_ACCEPT_LANGUAGE="fa")
    assert me.json()["preferences"]["language"] == "en"


def test_spa_shell_language_and_direction(admin_client):
    client = Client()
    page = client.get("/gozar/login").content.decode()
    assert '<html lang="en" dir="ltr">' in page
    assert '"defaultCalendar": "gregorian"' in page
    page = client.get("/gozar/login", HTTP_ACCEPT_LANGUAGE="fa").content.decode()
    assert '<html lang="fa" dir="rtl">' in page
    patch(admin_client, {"language": "fa", "calendar": "jalali"})
    page = admin_client.get("/gozar/").content.decode()
    assert '<html lang="fa" dir="rtl">' in page and '"calendar": "jalali"' in page


def test_timezone_and_home_dashboard_options(admin_client):
    body = patch(admin_client, {"options": {"timezone": "Europe/Berlin"}}).json()
    assert body["timezone"] == "Europe/Berlin"
    assert patch(admin_client, {"options": {"timezone": "Mars/Olympus"}}).status_code == 400
    assert patch(admin_client, {"options": {"home_dashboard": "x"}}).status_code == 400
    body = patch(admin_client, {"options": {"home_dashboard": 3, "timezone": ""}}).json()
    assert body["options"]["home_dashboard"] == 3 and body["timezone"] == "Asia/Tehran"
