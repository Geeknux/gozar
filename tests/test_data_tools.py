"""M6.5: saved list views, global search, recent actions and field-level history."""

import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client

from bi_admin.models import ChangeLog, SavedListView
from shop.models import City, Customer, Order

API = "/gozar/api"
pytestmark = pytest.mark.django_db
TEHRAN_REGION = ["Tehran", "Karaj", "Qom"]


def crud(label, suffix=""):
    app, model = label.split(".")
    return f"{API}/crud/{app}/{model}/{suffix}"


def login(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


# --- saved list views ----------------------------------------------------------------


def test_saved_views_crud(admin_client, seeded):
    resp = admin_client.post(
        f"{API}/list-views",
        {
            "model": "shop.order",
            "name": "Paid, newest",
            "params": {"status": "paid", "ordering": "-created_at", "page": "3"},
            "columns": ["__str__", "amount", "customer"],
            "is_default": True,
        },
        "application/json",
    )
    assert resp.status_code == 201, resp.json()
    view = resp.json()
    assert view["params"] == {"status": "paid", "ordering": "-created_at"}  # page dropped
    assert view["columns"] == ["__str__", "amount", "customer"]

    second = admin_client.post(
        f"{API}/list-views",
        {
            "model": "shop.order",
            "name": "Pending",
            "params": {"status": "pending"},
            "is_default": True,
        },
        "application/json",
    ).json()
    views = admin_client.get(f"{API}/list-views", {"model": "shop.order"}).json()["views"]
    assert [(v["name"], v["is_default"]) for v in views] == [
        ("Paid, newest", False),
        ("Pending", True),
    ]

    renamed = admin_client.patch(
        f"{API}/list-views/{view['id']}", {"name": "Paid"}, "application/json"
    ).json()
    assert renamed["name"] == "Paid" and renamed["params"]["status"] == "paid"
    dup = admin_client.patch(
        f"{API}/list-views/{view['id']}", {"name": "Pending"}, "application/json"
    )
    assert dup.status_code == 400 and "name" in dup.json()["errors"]
    assert admin_client.delete(f"{API}/list-views/{second['id']}").status_code == 204
    assert SavedListView.objects.count() == 1

    # the stored query string runs like any list request
    data = admin_client.get(
        crud("shop.order"), {**renamed["params"], "columns": ",".join(renamed["columns"])}
    )
    assert data.status_code == 200
    assert [c["name"] for c in data.json()["columns"]] == ["__str__", "amount", "customer"]


def test_saved_views_validate_and_stay_private(admin_client, make_staff, seeded):
    bad = admin_client.post(
        f"{API}/list-views",
        {"model": "shop.order", "name": "x", "columns": ["amount", "nope"]},
        "application/json",
    )
    assert bad.status_code == 400 and "columns" in bad.json()["errors"]
    assert (
        admin_client.post(
            f"{API}/list-views", {"model": "shop.order", "name": ""}, "application/json"
        ).status_code
        == 400
    )
    view = admin_client.post(
        f"{API}/list-views", {"model": "shop.order", "name": "Mine"}, "application/json"
    ).json()

    other = login(make_staff("shop.view_order"))
    assert other.get(f"{API}/list-views", {"model": "shop.order"}).json()["views"] == []
    assert (
        other.patch(f"{API}/list-views/{view['id']}", {"name": "x"}, "application/json").status_code
        == 404
    )
    assert other.delete(f"{API}/list-views/{view['id']}").status_code == 404

    no_access = login(make_staff("shop.view_city", username="cities"))
    assert no_access.get(f"{API}/list-views", {"model": "shop.order"}).status_code == 403
    assert (
        no_access.post(
            f"{API}/list-views", {"model": "shop.order", "name": "x"}, "application/json"
        ).status_code
        == 403
    )


# --- global search -------------------------------------------------------------------


def test_global_search_groups_hits_by_model(admin_client, seeded):
    customer = Customer.objects.select_related("city").first()
    last_name = customer.name.split()[-1]
    data = admin_client.get(f"{API}/search", {"q": last_name}).json()
    groups = {g["model"]: g for g in data["groups"]}
    assert "shop.customer" in groups
    expected = Customer.objects.filter(name__icontains=last_name).count()
    assert groups["shop.customer"]["count"] == expected
    assert len(groups["shop.customer"]["results"]) == min(expected, 5)
    # orders are searchable by customer name too (OrderOptions.search_fields)
    assert (
        groups["shop.order"]["count"]
        == Order.objects.filter(customer__name__icontains=last_name).count()
    )
    assert data["total"] == sum(g["count"] for g in data["groups"])
    assert admin_client.get(f"{API}/search", {"q": "a"}).json()["groups"] == []  # too short
    assert admin_client.get(f"{API}/search", {"q": "x" * 201}).status_code == 400


def test_global_search_respects_permissions_and_scope(regional, seeded):
    client = login(regional())
    data = client.get(f"{API}/search", {"q": "an", "per_model": 20}).json()
    assert data["groups"]
    for group in data["groups"]:
        if group["model"] == "shop.city":
            labels = {r["label"] for r in group["results"]}
            assert labels <= set(TEHRAN_REGION)
        if group["model"] == "shop.customer":
            pks = [r["pk"] for r in group["results"]]
            assert set(
                Customer.objects.filter(pk__in=pks).values_list("city__name_en", flat=True)
            ) <= set(TEHRAN_REGION)
    assert "auth.user" not in {g["model"] for g in data["groups"]}
    shiraz = client.get(f"{API}/search", {"q": "Shiraz"}).json()
    assert "shop.city" not in {g["model"] for g in shiraz["groups"]}


def test_global_search_persian_letters(admin_client, seeded_fa):
    data = admin_client.get(f"{API}/search", {"q": "كرج"}).json()  # Arabic kaf
    cities = next(g for g in data["groups"] if g["model"] == "shop.city")
    assert [r["label"] for r in cities["results"]] == ["کرج"]


# --- recent actions ------------------------------------------------------------------


def test_recent_actions_lists_own_edits_still_visible(admin_client, admin_user, seeded, make_staff):
    city = City.objects.get(name_en="Rasht")
    admin_client.patch(crud("shop.city", city.pk), {"province": "Gilan!"}, "application/json")
    gone = City.objects.create(name="Gone", province="X")
    admin_client.delete(crud("shop.city", gone.pk))
    entries = admin_client.get(f"{API}/meta/recent-actions").json()["entries"]
    assert [(e["action"], e["object_repr"], e["exists"]) for e in entries[:2]] == [
        ("delete", "Gone", False),
        ("change", "Rasht", True),
    ]
    assert entries[1]["verbose_name"] == "City" and entries[1]["model"] == "shop.city"

    # someone else's actions never show; nor do rows now outside your scope
    staff = make_staff("shop.view_city", "shop.change_city")
    staff.groups.add(Group.objects.get_or_create(name="Sales: Tehran region")[0])
    client = login(staff)
    assert client.get(f"{API}/meta/recent-actions").json()["entries"] == []
    client.patch(
        crud("shop.city", City.objects.get(name_en="Karaj").pk),
        {"province": "A"},
        "application/json",
    )
    ChangeLog.objects.create(
        user=staff,
        model_label="shop.city",
        object_pk=str(city.pk),
        object_repr="Rasht",
        action="change",
    )
    reprs = [e["object_repr"] for e in client.get(f"{API}/meta/recent-actions").json()["entries"]]
    assert reprs == ["Karaj"]


# --- history -------------------------------------------------------------------------


def test_history_has_field_level_diffs(admin_client, seeded):
    order = Order.objects.filter(status="pending").first()
    old_customer = order.customer
    new_customer = Customer.objects.exclude(pk=old_customer.pk).first()
    admin_client.patch(
        crud("shop.order", order.pk),
        {"status": "paid", "customer": new_customer.pk, "amount": "777"},
        "application/json",
    )
    (entry, *_rest) = admin_client.get(crud("shop.order", f"{order.pk}/history")).json()["entries"]
    diff = {d["field"]: d for d in entry["diff"]}
    assert diff["status"]["old"] == "Awaiting payment" and diff["status"]["new"] == "Paid"
    assert diff["status"]["label"] == "Status"
    assert diff["customer"]["old"]["label"] == old_customer.name
    assert diff["customer"]["new"]["pk"] == new_customer.pk
    assert diff["amount"]["new"] == "777" and diff["amount"]["kind"] == "decimal"


def test_history_hides_rows_now_outside_scope(admin_client, regional, seeded):
    order = Order.objects.filter(customer__city__name_en="Tehran").first()
    outsider = Customer.objects.exclude(city__name_en__in=TEHRAN_REGION).first()
    insider = Customer.objects.filter(city__name_en="Karaj").first()
    admin_client.patch(crud("shop.order", order.pk), {"customer": outsider.pk}, "application/json")
    admin_client.patch(crud("shop.order", order.pk), {"customer": insider.pk}, "application/json")
    client = login(regional())
    entries = client.get(crud("shop.order", f"{order.pk}/history")).json()["entries"]
    latest = {d["field"]: d for d in entries[0]["diff"]}["customer"]
    assert latest["new"]["label"] == insider.name
    assert latest["old"]["restricted"] is True and latest["old"]["label"] == "Restricted"
    assert outsider.name not in str(entries)


def test_history_never_records_passwords(admin_client, django_user_model):
    user = django_user_model.objects.create_user("pw", password="first-Pass-99!", is_staff=True)
    admin_client.patch(
        crud("auth.user", user.pk), {"password": "second-Pass-99!"}, "application/json"
    )
    entry = ChangeLog.objects.get(model_label="auth.user", object_pk=str(user.pk))
    assert entry.changes["fields"] == ["password"] and "diff" not in entry.changes
    assert "pbkdf2" not in str(entry.changes) and "md5" not in str(entry.changes)


def test_m2m_diff_lists_refs(admin_client, django_user_model):
    group = Group.objects.create(name="Editors")
    user = django_user_model.objects.create_user("m2m", is_staff=True)
    admin_client.patch(crud("auth.user", user.pk), {"groups": [group.pk]}, "application/json")
    entry = admin_client.get(crud("auth.user", f"{user.pk}/history")).json()["entries"][0]
    diff = {d["field"]: d for d in entry["diff"]}
    assert diff["groups"]["old"] == [] and diff["groups"]["new"][0]["label"] == "Editors"
    perms = Permission.objects.filter(codename="view_city")
    admin_client.patch(
        crud("auth.user", user.pk),
        {"user_permissions": list(perms.values_list("pk", flat=True))},
        "application/json",
    )
    entry = admin_client.get(crud("auth.user", f"{user.pk}/history")).json()["entries"][0]
    assert entry["diff"][0]["field"] == "user_permissions"
