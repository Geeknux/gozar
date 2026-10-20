"""M6.5: list features — list_editable, bulk edit, date hierarchy, column options,
sortable_by, show_full_result_count, chosen columns and select-all."""

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission

from bi_admin.calendar import jalali
from bi_admin.models import ChangeLog
from shop.models import Customer, Order, Product

API = "/gozar/api"
pytestmark = pytest.mark.django_db
TEHRAN_REGION = ["Tehran", "Karaj", "Qom"]


def crud(label, suffix=""):
    app, model = label.split(".")
    return f"{API}/crud/{app}/{model}/{suffix}"


def grant(user, *codenames):
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))


# --- columns -------------------------------------------------------------------------


def test_columns_describe_booleans_empty_values_and_editing(admin_client, seeded):
    data = admin_client.get(crud("shop.product")).json()
    cols = {c["name"]: c for c in data["columns"]}
    assert cols["is_active"]["boolean"] is True and cols["is_active"]["editable"] is True
    assert cols["price"]["editable"] is True
    assert cols["image_count"]["kind"] == "other" and cols["image_count"]["empty_value"] == "—"
    assert cols["name"]["is_link"] is True
    row = data["rows"][0]
    assert set(row["edit"]) == {"price", "is_active"}
    assert row["cells"][list(cols).index("image_count")] is None

    orders = admin_client.get(crud("shop.order")).json()
    paid = next(c for c in orders["columns"] if c["name"] == "is_paid")
    assert paid["boolean"] is True and paid["sortable"] is True  # admin_order_field
    i = [c["name"] for c in orders["columns"]].index("is_paid")
    for r in orders["rows"]:
        assert r["cells"][i] == (Order.objects.get(pk=r["pk"]).status == "paid")


def test_callable_column_sorts_by_its_ordering_field(admin_client, seeded):
    rows = admin_client.get(crud("shop.order"), {"ordering": "is_paid", "page_size": 500}).json()
    paid_at = [Order.objects.get(pk=r["pk"]).paid_at for r in rows["rows"]]
    filled = [p for p in paid_at if p is not None]
    assert filled == sorted(filled)
    assert admin_client.get(crud("shop.order"), {"ordering": "items_total"}).status_code == 400


def test_sortable_by_restricts_ordering(admin_client, seeded):
    data = admin_client.get(crud("shop.product")).json()
    sortable = {c["name"] for c in data["columns"] if c["sortable"]}
    assert sortable == {"name", "category", "price"}
    assert admin_client.get(crud("shop.product"), {"ordering": "-price"}).status_code == 200
    assert admin_client.get(crud("shop.product"), {"ordering": "is_active"}).status_code == 400


def test_show_full_result_count(admin_client, seeded, monkeypatch):
    from shop.gozar import ProductOptions

    data = admin_client.get(crud("shop.product"), {"category": "books"}).json()
    assert data["total"] == Product.objects.count()
    monkeypatch.setattr(ProductOptions, "show_full_result_count", False)
    data = admin_client.get(crud("shop.product"), {"category": "books"}).json()
    assert (
        data["total"] is None and data["count"] == Product.objects.filter(category="books").count()
    )
    # unfiltered lists still know their total without an extra query
    assert admin_client.get(crud("shop.product")).json()["total"] == Product.objects.count()


def test_list_select_related_options(
    admin_client, seeded, monkeypatch, django_assert_max_num_queries
):
    from shop.gozar import OrderOptions

    monkeypatch.setattr(OrderOptions, "list_display", ["__str__", "customer__city__name"])
    monkeypatch.setattr(OrderOptions, "list_editable", [])
    for value in (False, True, ["customer__city"]):
        monkeypatch.setattr(OrderOptions, "list_select_related", value)
        with django_assert_max_num_queries(12):
            assert admin_client.get(crud("shop.order"), {"page_size": 50}).status_code == 200


def test_chosen_columns(admin_client, seeded):
    meta = admin_client.get(f"{API}/meta/models/shop/customer").json()
    available = [c["name"] for c in meta["list"]["available_columns"]]
    assert available[:5] == ["name", "phone", "city", "city__province", "created_at"]
    data = admin_client.get(crud("shop.customer"), {"columns": "phone,name"}).json()
    assert [c["name"] for c in data["columns"]] == ["phone", "name"]
    assert data["rows"][0]["cells"][0] == Customer.objects.get(pk=data["rows"][0]["pk"]).phone
    assert admin_client.get(crud("shop.customer"), {"columns": "phone,secret"}).status_code == 400
    users = admin_client.get(crud("auth.user"), {"columns": "username,password"})
    assert users.status_code == 400  # never a password column


def test_search_help_text_and_meta(admin_client, seeded):
    meta = admin_client.get(f"{API}/meta/models/shop/order").json()
    assert meta["list"]["search_help_text"] == "Search by customer name or phone number."
    assert meta["list"]["editable"] == ["status"]
    assert meta["list"]["date_hierarchy"] == {"field": "created_at", "label": "Created at"}
    assert "status" in meta["list"]["bulk_edit_fields"]
    assert meta["form"]["save_on_top"] is True and meta["form"]["save_as"] is False


# --- list_editable -------------------------------------------------------------------


def test_list_editable_saves_rows_and_logs(admin_client, seeded):
    products = list(Product.objects.order_by("pk")[:2])
    resp = admin_client.patch(
        crud("shop.product"),
        {
            "rows": [
                {"pk": products[0].pk, "values": {"price": "123", "is_active": False}},
                {"pk": products[1].pk, "values": {"price": "456"}},
            ]
        },
        "application/json",
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["updated"] == 2
    assert {r["pk"]: r["edit"]["price"] for r in resp.json()["rows"]} == {
        products[0].pk: "123",
        products[1].pk: "456",
    }
    products[0].refresh_from_db()
    assert products[0].price == Decimal(123) and products[0].is_active is False
    entry = ChangeLog.objects.filter(
        model_label="shop.product", object_pk=str(products[0].pk)
    ).first()
    assert entry.changes["source"] == "list"
    assert entry.changes["diff"]["is_active"] == {"old": True, "new": False}


def test_list_editable_is_all_or_nothing_with_row_errors(admin_client, seeded):
    a, b = Product.objects.order_by("pk")[:2]
    resp = admin_client.patch(
        crud("shop.product"),
        {"rows": [{"pk": a.pk, "values": {"price": "1"}}, {"pk": b.pk, "values": {"price": "x"}}]},
        "application/json",
    )
    assert resp.status_code == 400
    assert set(resp.json()["row_errors"]) == {str(b.pk)}
    assert "price" in resp.json()["row_errors"][str(b.pk)]
    a.refresh_from_db()
    assert a.price != 1


def test_list_editable_only_its_fields_and_permission(client, admin_client, make_staff, seeded):
    p = Product.objects.first()
    resp = admin_client.patch(
        crud("shop.product"), {"rows": [{"pk": p.pk, "values": {"name": "x"}}]}, "application/json"
    )
    assert resp.status_code == 400
    assert "name" in resp.json()["row_errors"][str(p.pk)]
    assert (
        admin_client.patch(
            crud("shop.city"), {"rows": [{"pk": 1, "values": {}}]}, "application/json"
        ).status_code
        == 400
    )  # no list_editable
    client.force_login(make_staff("shop.view_product"))
    data = client.get(crud("shop.product")).json()
    assert "edit" not in data["rows"][0]
    resp = client.patch(
        crud("shop.product"), {"rows": [{"pk": p.pk, "values": {"price": "1"}}]}, "application/json"
    )
    assert resp.status_code == 403


def test_list_editable_respects_row_scope(client, regional, seeded):
    user = regional()
    grant(user, "change_order")
    client.force_login(user)
    inside = Order.objects.filter(
        customer__city__name_en__in=TEHRAN_REGION, status="pending"
    ).first()
    outside = Order.objects.exclude(customer__city__name_en__in=TEHRAN_REGION).first()
    resp = client.patch(
        crud("shop.order"),
        {
            "rows": [
                {"pk": inside.pk, "values": {"status": "cancelled"}},
                {"pk": outside.pk, "values": {"status": "cancelled"}},
            ]
        },
        "application/json",
    )
    assert resp.status_code == 400
    assert set(resp.json()["row_errors"]) == {str(outside.pk)}
    inside.refresh_from_db()
    assert inside.status == "pending"  # nothing saved
    ok = client.patch(
        crud("shop.order"),
        {"rows": [{"pk": inside.pk, "values": {"status": "cancelled"}}]},
        "application/json",
    )
    assert ok.status_code == 200
    inside.refresh_from_db()
    assert inside.status == "cancelled"


# --- bulk edit -----------------------------------------------------------------------


def test_bulk_edit_selected_rows(admin_client, seeded):
    pks = list(Product.objects.order_by("pk").values_list("pk", flat=True)[:3])
    resp = admin_client.post(
        crud("shop.product", "bulk-edit"),
        {"pks": pks, "values": {"category": "books", "is_active": False}},
        "application/json",
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["updated"] == 3
    assert set(Product.objects.filter(pk__in=pks).values_list("category", "is_active")) == {
        ("books", False)
    }
    assert ChangeLog.objects.filter(model_label="shop.product", changes__source="bulk").count() == 3


def test_bulk_edit_select_all_matching_the_list(admin_client, seeded):
    total = Product.objects.filter(category="grocery").count()
    resp = admin_client.post(
        crud("shop.product", "bulk-edit"),
        {"all": True, "params": {"category": "grocery"}, "values": {"is_active": False}},
        "application/json",
    )
    assert resp.status_code == 200 and resp.json()["updated"] == total
    assert not Product.objects.filter(category="grocery", is_active=True).exists()
    assert Product.objects.exclude(category="grocery").filter(is_active=True).exists()


def test_bulk_edit_validation_rolls_back(admin_client, seeded):
    pks = list(Product.objects.values_list("pk", flat=True)[:2])
    resp = admin_client.post(
        crud("shop.product", "bulk-edit"),
        {"pks": pks, "values": {"price": "-x"}},
        "application/json",
    )
    assert resp.status_code == 400 and set(resp.json()["row_errors"]) == {str(p) for p in pks}
    bad_field = admin_client.post(
        crud("shop.product", "bulk-edit"),
        {"pks": pks, "values": {"datasheet": None}},
        "application/json",
    )
    assert bad_field.status_code == 400
    empty = admin_client.post(
        crud("shop.product", "bulk-edit"), {"pks": pks, "values": {}}, "application/json"
    )
    assert empty.status_code == 400


def test_bulk_edit_scope_and_limit(client, regional, seeded, settings):
    user = regional()
    grant(user, "change_order")
    client.force_login(user)
    pks = list(Order.objects.values_list("pk", flat=True))
    visible = set(
        Order.objects.filter(customer__city__name_en__in=TEHRAN_REGION).values_list("pk", flat=True)
    )
    outside = dict(Order.objects.exclude(pk__in=visible).values_list("pk", "status"))
    resp = client.post(
        crud("shop.order", "bulk-edit"),
        {"pks": pks, "values": {"status": "refunded"}},
        "application/json",
    )
    assert resp.status_code == 200
    assert set(resp.json()["pks"]) == visible  # the selection is intersected with the scope
    assert dict(Order.objects.exclude(pk__in=visible).values_list("pk", "status")) == outside

    settings.GOZAR = {**settings.GOZAR, "BULK_EDIT_MAX_ROWS": 2}
    resp = client.post(
        crud("shop.order", "bulk-edit"),
        {"all": True, "params": {}, "values": {"status": "paid"}},
        "application/json",
    )
    assert resp.status_code == 400 and resp.json()["code"] == "too_many_rows"


def test_actions_accept_select_all(admin_client, seeded):
    pending = Order.objects.filter(status="pending").count()
    resp = admin_client.post(
        crud("shop.order", "actions/mark_cancelled"),
        {"all": True, "params": {"status": "pending"}},
        "application/json",
    )
    assert resp.status_code == 200 and resp.json()["updated"] == pending
    plan = admin_client.post(
        crud("shop.product", "delete-plan"),
        {"all": True, "params": {"category": "books"}},
        "application/json",
    ).json()
    summary = {s["model"]: s["count"] for s in plan["summary"]}
    assert summary["shop.product"] == Product.objects.filter(category="books").count()


# --- date hierarchy ------------------------------------------------------------------


def test_date_hierarchy_gregorian_levels(admin_client, orders):
    data = admin_client.get(crud("shop.order"), {"dh_calendar": "gregorian"}).json()
    dh = data["date_hierarchy"]
    assert dh["level"] == "year" and dh["calendar"] == "gregorian"
    assert {b["year"]: b["count"] for b in dh["buckets"]} == {2025: 1, 2026: 8}

    data = admin_client.get(
        crud("shop.order"), {"dh_calendar": "gregorian", "dh_year": 2026}
    ).json()
    assert data["count"] == 8
    months = {b["month"]: b["count"] for b in data["date_hierarchy"]["buckets"]}
    assert months == {3: 2, 8: 2, 9: 3, 10: 1}

    data = admin_client.get(
        crud("shop.order"), {"dh_calendar": "gregorian", "dh_year": 2026, "dh_month": 9}
    ).json()
    days = {b["day"]: b["count"] for b in data["date_hierarchy"]["buckets"]}
    assert days == {22: 1, 23: 1, 30: 1}  # local (Tehran) dates
    data = admin_client.get(
        crud("shop.order"),
        {"dh_calendar": "gregorian", "dh_year": 2026, "dh_month": 9, "dh_day": 23},
    ).json()
    assert data["count"] == 1 and data["date_hierarchy"]["level"] is None


def test_date_hierarchy_jalali_buckets_follow_jalali_months(admin_client, orders):
    data = admin_client.get(crud("shop.order"), {"dh_calendar": "jalali"}).json()
    assert {b["year"]: b["count"] for b in data["date_hierarchy"]["buckets"]} == {1404: 2, 1405: 7}
    data = admin_client.get(crud("shop.order"), {"dh_calendar": "jalali", "dh_year": 1405}).json()
    months = {b["month"]: b["count"] for b in data["date_hierarchy"]["buckets"]}
    # 1405/06/31 (last day of summer) and 1405/07/01 fall in different Jalali months
    assert months == {1: 1, 5: 1, 6: 2, 7: 3}
    start = next(b for b in data["date_hierarchy"]["buckets"] if b["month"] == 7)["start"]
    assert start == jalali.to_gregorian(1405, 7, 1).isoformat()
    assert data["date_hierarchy"]["start"] == jalali.to_gregorian(1405, 1, 1).isoformat()


def test_date_hierarchy_defaults_to_the_users_calendar_and_combines_with_filters(
    admin_client, admin_user, orders
):
    from bi_admin.models import UserPreferences

    UserPreferences.objects.create(user=admin_user, calendar="jalali")
    data = admin_client.get(crud("shop.order"), {"dh_year": 1405, "status": "paid"}).json()
    assert data["date_hierarchy"]["calendar"] == "jalali"
    assert data["count"] == 4  # 1405: orders 1, 2, 4 and 8 are paid
    assert admin_client.get(crud("shop.order"), {"dh_month": 3}).status_code == 400
    assert admin_client.get(crud("shop.order"), {"dh_year": "x"}).status_code == 400
    assert (
        admin_client.get(crud("shop.order"), {"dh_year": 1405, "dh_month": 13}).status_code == 400
    )


def test_date_hierarchy_respects_scope(client, regional, orders):
    client.force_login(regional())
    data = client.get(crud("shop.order"), {"dh_calendar": "gregorian"}).json()
    total = sum(b["count"] for b in data["date_hierarchy"]["buckets"])
    assert total == Order.objects.filter(customer__city__name_en="Tehran").count()


def test_hierarchy_ranges_unit():
    from bi_admin.crud import hierarchy

    assert hierarchy._range("gregorian", 2024, 2, None) == (
        dt.date(2024, 2, 1),
        dt.date(2024, 3, 1),
    )
    assert hierarchy._range("gregorian", 2024, 12, None)[1] == dt.date(2025, 1, 1)
    assert hierarchy._range("jalali", 1403, 12, None) == (
        jalali.to_gregorian(1403, 12, 1),
        jalali.to_gregorian(1404, 1, 1),
    )
    start, end = hierarchy._range("jalali", 1403, 12, 30)  # 1403 is a leap year
    assert (end - start).days == 1
    assert hierarchy._days_in(2024, 2) == 29
