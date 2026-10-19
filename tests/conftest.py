import datetime as dt
from decimal import Decimal
from io import StringIO
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import Permission
from django.core.management import call_command

from bi_admin.crud import site
from bi_admin.discovery import reset_registry
from bi_admin.semantic import semantic_site

API = "/gozar/api"


@pytest.fixture(autouse=True)
def _fast_hashing(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture(autouse=True)
def _fresh_cache(settings):
    """Report results and data versions must not leak between tests."""
    from django.core.cache import cache

    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_registry()
    site.clear_cache()
    semantic_site.clear_cache()
    yield
    reset_registry()
    site.clear_cache()
    semantic_site.clear_cache()


@pytest.fixture
def seeded(db):
    """The demo data set in the default (English) locale."""
    call_command("seed_demo", customers=20, orders=60, seed=7, stdout=StringIO())


@pytest.fixture
def seeded_fa(db):
    """The same demo data set with Persian names (``seed_demo --locale fa``)."""
    call_command("seed_demo", customers=20, orders=60, seed=7, locale="fa", stdout=StringIO())


@pytest.fixture
def admin_user(django_user_model):
    return django_user_model.objects.create_superuser("root", "root@example.com", "s3cret-pass!")


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def make_staff(django_user_model):
    def make(*perms: str, username="staff"):
        user = django_user_model.objects.create_user(
            username, password="x-pass-123!", is_staff=True
        )
        for perm in perms:
            app_label, codename = perm.split(".")
            user.user_permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        return user

    return make


# --- report fixtures (M2) ----------------------------------------------------------

TEHRAN = ZoneInfo("Asia/Tehran")
TODAY = dt.date(2026, 10, 18)  # = 1405/07/26 (a Sunday)


def tehran(y, m, d, hh=12, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=TEHRAN)


@pytest.fixture
def calendar_table(db):
    from bi_admin.calendar import table

    table.populate(dt.date(2025, 1, 1), dt.date(2026, 12, 31))


@pytest.fixture
def frozen_today(monkeypatch):
    from bi_admin.calendar import periods

    monkeypatch.setattr(periods, "today", lambda: TODAY)
    return TODAY


@pytest.fixture
def orders(db):
    """Nine hand-made orders whose report numbers are computed by hand in the tests.

    #  customer/city    status     amount  created (Tehran time)  Jalali
    1  c1 Tehran        paid        100    2026-09-22 23:30       1405/06/31 (last day of summer)
    2  c1 Tehran        paid        200    2026-09-23 00:30       1405/07/01 (first day of autumn)
    3  c2 Tehran        pending      50    2026-09-30 10:00       1405/07/08
    4  c3 Shiraz        paid        300    2026-10-01 09:00       1405/07/09
    5  c3 Shiraz        cancelled    70    2026-08-23 08:00       1405/06/01
    6  c4 Mashhad       refunded    400    2026-08-22 20:00       1405/05/31
    7  c2 Tehran        paid        500    2026-03-20 12:00       1404/12/29 (last day of 1404)
    8  c4 Mashhad       paid        600    2026-03-21 12:00       1405/01/01
    9  c1 Tehran        paid       1000    2025-10-18 12:00       1404/07/26
    """
    from shop.models import City, Customer, Order

    tehran_city = City.objects.create(name="تهران", name_en="Tehran", province="تهران")
    shiraz = City.objects.create(name="شیراز", name_en="Shiraz", province="فارس")
    mashhad = City.objects.create(name="مشهد", name_en="Mashhad", province="خراسان رضوی")
    joined = tehran(2024, 1, 1)
    c1 = Customer.objects.create(name="علی", city=tehran_city, created_at=joined)
    c2 = Customer.objects.create(name="زهرا", city=tehran_city, created_at=joined)
    c3 = Customer.objects.create(name="رضا", city=shiraz, created_at=joined)
    c4 = Customer.objects.create(name="سارا", city=mashhad, created_at=joined)
    rows = [
        (c1, "paid", 100, tehran(2026, 9, 22, 23, 30)),
        (c1, "paid", 200, tehran(2026, 9, 23, 0, 30)),
        (c2, "pending", 50, tehran(2026, 9, 30, 10)),
        (c3, "paid", 300, tehran(2026, 10, 1, 9)),
        (c3, "cancelled", 70, tehran(2026, 8, 23, 8)),
        (c4, "refunded", 400, tehran(2026, 8, 22, 20)),
        (c2, "paid", 500, tehran(2026, 3, 20)),
        (c4, "paid", 600, tehran(2026, 3, 21)),
        (c1, "paid", 1000, tehran(2025, 10, 18)),
    ]
    return [
        Order.objects.create(
            customer=c,
            status=status,
            amount=Decimal(amount),
            created_at=created,
            paid_at=created + dt.timedelta(minutes=5) if status in ("paid", "refunded") else None,
        )
        for c, status, amount, created in rows
    ]


# --- access control fixtures (M4) ----------------------------------------------------

SHOP_VIEW = [f"shop.view_{m}" for m in ("city", "customer", "product", "order", "orderitem")]
TEHRAN_REGION = ["Tehran", "Karaj", "Qom"]  # shop.gozar.REGIONS["Sales: Tehran region"]


@pytest.fixture
def regional(make_staff):
    """A staff user in a sales-region group (row rules limit them to its cities)."""
    from django.contrib.auth.models import Group

    def make(region="Sales: Tehran region", *perms, username="tehran"):
        user = make_staff(*(perms or SHOP_VIEW), username=username)
        user.groups.add(Group.objects.get_or_create(name=region)[0])
        return user

    return make


@pytest.fixture
def report_api():
    """``report_api(client, "run", spec, cache="false")`` → POST /reports/<path>."""

    def post(client, path, body, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{API}/reports/{path}{'?' + query if query else ''}"
        return client.post(url, body, "application/json")

    return post
