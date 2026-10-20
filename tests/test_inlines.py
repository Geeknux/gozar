"""M6.5: inlines — metadata, saving with the parent in one transaction, validation,
per-inline permissions and row-level scope."""

from decimal import Decimal

import pytest
from django.db.models import Q

from bi_admin.access import rules
from bi_admin.models import ChangeLog
from shop.models import Order, OrderItem, Product

API = "/gozar/api"
pytestmark = pytest.mark.django_db


def crud(label, suffix=""):
    app, model = label.split(".")
    return f"{API}/crud/{app}/{model}/{suffix}"


def items_payload(*rows):
    return {"__inlines__": {"items": list(rows)}}


@pytest.fixture
def order(seeded):
    order = Order.objects.filter(items__isnull=False).order_by("pk").first()
    assert order.items.count() >= 1
    return order


def test_meta_describes_inlines(admin_client, seeded):
    meta = admin_client.get(f"{API}/meta/models/shop/order").json()
    (inline,) = meta["form"]["inlines"]
    assert inline["name"] == "items" and inline["model"] == "shop.orderitem"
    assert inline["style"] == "tabular" and inline["extra"] == 1
    assert inline["fields"] == ["product", "quantity", "unit_price", "line_total"]
    assert "line_total" in inline["readonly_fields"]
    assert inline["widgets"]["product"] == {"type": "autocomplete"}
    assert inline["permissions"] == {"view": True, "add": True, "change": True, "delete": True}
    assert inline["defaults"] == {"product": None, "quantity": 1, "unit_price": None}
    computed = next(f for f in inline["field_meta"] if f["name"] == "line_total")
    assert computed["computed"] and computed["verbose_name"] == "line total"

    product = admin_client.get(f"{API}/meta/models/shop/product").json()
    (images,) = product["form"]["inlines"]
    assert images["style"] == "stacked" and images["max_num"] == 6


def test_detail_lists_inline_rows_with_computed_values(admin_client, order):
    data = admin_client.get(crud("shop.order", order.pk)).json()
    rows = data["inlines"]["items"]["rows"]
    assert {r["pk"] for r in rows} == set(order.items.values_list("pk", flat=True))
    first = OrderItem.objects.get(pk=rows[0]["pk"])
    assert rows[0]["values"]["line_total"] == str(first.quantity * first.unit_price)
    assert rows[0]["values"]["product"]["model"] == "shop.product"
    assert data["inlines"]["items"]["hidden"] == 0
    assert data["values"]["items_total"] == str(
        sum(i.quantity * i.unit_price for i in order.items.all())
    )


def test_update_adds_changes_and_deletes_rows_and_logs(admin_client, order):
    items = list(order.items.order_by("pk"))
    product = Product.objects.exclude(pk__in=[i.product_id for i in items]).first()
    keep, *others = items
    quantity = keep.quantity
    rows = [
        {"pk": keep.pk, "values": {"quantity": quantity + 5}},
        {"pk": None, "values": {"product": product.pk, "quantity": 2, "unit_price": "1000"}},
    ]
    rows += [{"pk": o.pk, "delete": True} for o in others]
    resp = admin_client.patch(
        crud("shop.order", order.pk), items_payload(*rows), "application/json"
    )
    assert resp.status_code == 200, resp.json()
    order.refresh_from_db()
    assert order.items.count() == 2
    keep.refresh_from_db()
    assert keep.quantity == quantity + 5
    new = order.items.get(product=product)
    assert (new.quantity, new.unit_price) == (2, Decimal(1000))

    entry = ChangeLog.objects.filter(model_label="shop.order", object_pk=str(order.pk)).first()
    (summary,) = entry.changes["inlines"]
    assert summary["name"] == "items"
    assert summary["added"] == [str(new)]
    assert summary["changed"][0]["fields"] == ["quantity"]
    assert summary["changed"][0]["diff"]["quantity"] == {"old": quantity, "new": quantity + 5}
    assert len(summary["deleted"]) == len(others)
    # the inline rows get their own history too
    assert ChangeLog.objects.filter(
        model_label="shop.orderitem", object_pk=str(new.pk), action="add"
    ).exists()
    history = admin_client.get(crud("shop.order", f"{order.pk}/history")).json()["entries"]
    assert history[0]["inlines"][0]["changed"][0]["diff"][0]["field"] == "quantity"


def test_rows_left_out_are_untouched(admin_client, order):
    before = list(order.items.values_list("pk", "quantity"))
    resp = admin_client.patch(
        crud("shop.order", order.pk), {"status": "paid", **items_payload()}, "application/json"
    )
    assert resp.status_code == 200
    assert list(order.items.values_list("pk", "quantity")) == before


def test_create_parent_with_inline_rows_in_one_request(admin_client, seeded):
    from shop.models import Customer

    customer = Customer.objects.first()
    product = Product.objects.first()
    body = {
        "customer": customer.pk,
        "status": "pending",
        "amount": "5000",
        "created_at": "2026-10-01T10:00:00+03:30",
        **items_payload(
            {"values": {"product": product.pk, "quantity": 3, "unit_price": "100"}},
            {"values": {}},  # an untouched extra row is skipped, like admin's extra forms
        ),
    }
    resp = admin_client.post(crud("shop.order"), body, "application/json")
    assert resp.status_code == 201, resp.json()
    order = Order.objects.get(pk=resp.json()["pk"])
    assert list(order.items.values_list("product", "quantity")) == [(product.pk, 3)]
    assert resp.json()["inlines"]["items"]["rows"][0]["values"]["quantity"] == 3


def test_an_invalid_row_rolls_back_the_parent_and_every_row(admin_client, order):
    items = list(order.items.order_by("pk"))
    status = order.status
    resp = admin_client.patch(
        crud("shop.order", order.pk),
        {
            "status": "refunded" if status != "refunded" else "paid",
            **items_payload(
                {"pk": items[0].pk, "values": {"quantity": 99}},
                {"values": {"product": None, "quantity": "many"}},
            ),
        },
        "application/json",
    )
    assert resp.status_code == 400
    errors = resp.json()["inline_errors"]["items"]["rows"]
    assert set(errors) == {"1"}
    assert {"product", "quantity"} <= set(errors["1"])
    order.refresh_from_db()
    assert order.status == status
    items[0].refresh_from_db()
    assert items[0].quantity != 99
    assert order.items.count() == len(items)


def test_unknown_row_and_unknown_inline_and_bad_fields(admin_client, order):
    other = OrderItem.objects.exclude(order=order).first()
    quantity = other.quantity
    resp = admin_client.patch(
        crud("shop.order", order.pk),
        items_payload({"pk": other.pk, "values": {"quantity": 1}}),
        "application/json",
    )
    assert resp.status_code == 400
    assert resp.json()["inline_errors"]["items"]["rows"]["0"]["__all__"]
    other.refresh_from_db()
    assert other.quantity == quantity  # never touched: it belongs to another order

    resp = admin_client.patch(
        crud("shop.order", order.pk), {"__inlines__": {"nope": []}}, "application/json"
    )
    assert resp.status_code == 400 and resp.json()["code"] == "unknown_inline"

    item = order.items.first()
    resp = admin_client.patch(
        crud("shop.order", order.pk),
        items_payload({"pk": item.pk, "values": {"order": 1, "line_total": 5}}),
        "application/json",
    )
    assert resp.status_code == 400
    assert set(resp.json()["inline_errors"]["items"]["rows"]["0"]) == {"order", "line_total"}


def test_min_and_max_num(admin_client, order, monkeypatch):
    from shop.gozar import OrderItemInline

    monkeypatch.setattr(OrderItemInline, "min_num", 1)
    rows = [{"pk": i.pk, "delete": True} for i in order.items.all()]
    resp = admin_client.patch(
        crud("shop.order", order.pk), items_payload(*rows), "application/json"
    )
    assert resp.status_code == 400
    assert resp.json()["inline_errors"]["items"]["non_form"]

    monkeypatch.setattr(OrderItemInline, "min_num", None)
    monkeypatch.setattr(OrderItemInline, "max_num", order.items.count())
    product = Product.objects.first()
    resp = admin_client.patch(
        crud("shop.order", order.pk),
        items_payload({"values": {"product": product.pk, "quantity": 1, "unit_price": "1"}}),
        "application/json",
    )
    assert resp.status_code == 400
    assert resp.json()["inline_errors"]["items"]["non_form"]


def test_inline_permissions_are_the_inline_models(client, make_staff, order):
    user = make_staff("shop.change_order", "shop.view_orderitem", "shop.change_orderitem")
    client.force_login(user)
    meta = client.get(f"{API}/meta/models/shop/order").json()
    assert meta["form"]["inlines"][0]["permissions"] == {
        "view": True,
        "add": False,
        "change": True,
        "delete": False,
    }
    item = order.items.first()
    product = Product.objects.first()
    ok = client.patch(
        crud("shop.order", order.pk),
        items_payload({"pk": item.pk, "values": {"quantity": 7}}),
        "application/json",
    )
    assert ok.status_code == 200, ok.json()
    for row in (
        {"values": {"product": product.pk, "quantity": 1, "unit_price": "1"}},
        {"pk": item.pk, "delete": True},
    ):
        resp = client.patch(crud("shop.order", order.pk), items_payload(row), "application/json")
        assert resp.status_code == 400
        assert resp.json()["inline_errors"]["items"]["rows"]["0"]["__all__"]
    assert OrderItem.objects.filter(pk=item.pk).exists()

    # without view permission on the inline model, the inline isn't offered or accepted
    viewer = make_staff("shop.change_order", username="no-items")
    client.force_login(viewer)
    meta = client.get(f"{API}/meta/models/shop/order").json()
    assert meta["form"]["inlines"] == []
    assert client.get(crud("shop.order", order.pk)).json()["inlines"] == {}
    resp = client.patch(
        crud("shop.order", order.pk),
        items_payload({"pk": item.pk, "values": {"quantity": 1}}),
        "application/json",
    )
    assert resp.status_code == 403


@pytest.fixture
def no_electronics():
    """A row rule hiding electronics order items (on top of the demo region rules)."""

    def rule(user):
        return (
            Q(product__category="electronics")
            if user.is_superuser
            else ~Q(product__category="electronics")
        )

    rules.rule(OrderItem, rule)
    yield
    rules.registry.remove(OrderItem, rule)


def test_hidden_inline_rows_are_counted_never_shown_or_touched(
    client, make_staff, seeded, no_electronics
):
    order = (
        Order.objects.filter(items__product__category="electronics")
        .filter(items__product__category__in=["books", "grocery", "clothing", "home"])
        .first()
    )
    user = make_staff(
        "shop.change_order", "shop.view_orderitem", "shop.change_orderitem", "shop.delete_orderitem"
    )
    client.force_login(user)
    data = client.get(crud("shop.order", order.pk)).json()
    visible = order.items.exclude(product__category="electronics")
    hidden = order.items.filter(product__category="electronics")
    assert {r["pk"] for r in data["inlines"]["items"]["rows"]} == set(
        visible.values_list("pk", flat=True)
    )
    assert data["inlines"]["items"]["hidden"] == hidden.count()

    # deleting every visible row leaves the hidden ones alone
    rows = [{"pk": i.pk, "delete": True} for i in visible]
    resp = client.patch(crud("shop.order", order.pk), items_payload(*rows), "application/json")
    assert resp.status_code == 200, resp.json()
    assert set(order.items.values_list("pk", flat=True)) == set(hidden.values_list("pk", flat=True))

    # a hidden row can't be addressed
    resp = client.patch(
        crud("shop.order", order.pk),
        items_payload({"pk": hidden.first().pk, "values": {"quantity": 50}}),
        "application/json",
    )
    assert resp.status_code == 400


def test_a_row_moved_out_of_scope_rolls_everything_back(client, make_staff, seeded, no_electronics):
    order = (
        Order.objects.exclude(items__product__category="electronics")
        .filter(items__isnull=False)
        .first()
    )
    user = make_staff("shop.change_order", "shop.view_orderitem", "shop.change_orderitem")
    client.force_login(user)
    item = order.items.first()
    phone = Product.objects.filter(category="electronics").first()
    before = order.status
    resp = client.patch(
        crud("shop.order", order.pk),
        {
            "status": "cancelled" if before != "cancelled" else "paid",
            **items_payload({"pk": item.pk, "values": {"product": phone.pk}}),
        },
        "application/json",
    )
    # products aren't ruled, so the form accepts it; the post-save scope check
    # rejects the row and rolls the whole save back
    assert resp.status_code == 403 and resp.json()["code"] == "out_of_scope"
    item.refresh_from_db()
    order.refresh_from_db()
    assert item.product_id != phone.pk and order.status == before


def test_region_user_sees_and_edits_only_their_rows(client, regional, seeded):
    from django.contrib.auth.models import Permission

    user = regional()
    user.user_permissions.add(
        *Permission.objects.filter(codename__in=["change_order", "change_orderitem"])
    )
    client.force_login(user)
    outside = Order.objects.exclude(customer__city__name_en__in=["Tehran", "Karaj", "Qom"]).first()
    assert client.get(crud("shop.order", outside.pk)).status_code == 404
    assert (
        client.patch(
            crud("shop.order", outside.pk),
            items_payload({"pk": outside.items.first().pk, "delete": True}),
            "application/json",
        ).status_code
        == 404
    )


def test_inline_autocomplete(admin_client, order):
    resp = admin_client.get(
        crud("shop.order", "autocomplete"), {"field": "product", "inline": "items", "q": "sa"}
    )
    assert resp.status_code == 200
    assert all("sa" in r["label"].lower() for r in resp.json()["results"])
    bad = admin_client.get(
        crud("shop.order", "autocomplete"), {"field": "quantity", "inline": "items"}
    )
    assert bad.status_code == 400
    limited = admin_client.get(
        crud("shop.product", "autocomplete"), {"field": "tags", "limit": 500}
    )
    assert limited.status_code == 200
