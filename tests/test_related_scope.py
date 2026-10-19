"""Related rows outside the user's row-level scope never show in serialized
objects or list cells, and saving a form that only shows the visible subset of
an M2M field keeps the hidden links."""

import pytest
from django.contrib.auth.models import Group
from django.db.models import Q
from django.test import Client

from bi_admin.access import rule
from bi_admin.access.rules import registry
from bi_admin.crud import ModelOptions, site
from shop.models import Customer, Order, OrderItem, Product

pytestmark = pytest.mark.django_db

API = "/gozar/api"


def crud(label, suffix=""):
    app, model = label.split(".")
    return f"{API}/crud/{app}/{model}/{suffix}"


@pytest.fixture
def temp_rule():
    added = []

    def add(target, predicate):
        rule(target, predicate)
        added.append((target, predicate))

    yield add
    for target, predicate in added:
        registry.remove(target, predicate)


@pytest.fixture
def groups_setup(make_staff, django_user_model, temp_rule):
    """Staff may see groups named "Visible*" only; the target user is in both kinds."""
    visible, other = Group.objects.create(name="Visible A"), Group.objects.create(name="Visible B")
    hidden = Group.objects.create(name="Hidden")
    target = django_user_model.objects.create_user("target", password="pw-123456!")
    target.groups.set([visible, hidden])
    temp_rule(Group, lambda user: Q(name__startswith="Visible"))
    client = Client()
    client.force_login(make_staff("auth.view_user", "auth.change_user", "auth.view_group"))
    return client, target, visible, other, hidden


def group_names(user):
    return sorted(user.groups.values_list("name", flat=True))


def test_m2m_lists_only_visible_links(groups_setup):
    client, target, visible, _other, _hidden = groups_setup
    body = client.get(crud("auth.user", target.pk)).json()
    assert [g["label"] for g in body["values"]["groups"]] == ["Visible A"]
    assert body["hidden_links"] == {"groups": 1}


def test_saving_the_visible_subset_keeps_hidden_links(groups_setup):
    client, target, visible, other, hidden = groups_setup
    patch = lambda body: client.patch(crud("auth.user", target.pk), body, "application/json")  # noqa: E731
    # what the SPA sends back: the visible subset, unchanged
    assert patch({"groups": [{"pk": visible.pk}]}).status_code == 200
    assert group_names(target) == ["Hidden", "Visible A"]
    assert patch({"groups": [visible.pk, other.pk]}).status_code == 200
    assert group_names(target) == ["Hidden", "Visible A", "Visible B"]
    assert patch({"groups": []}).status_code == 200  # removes the visible ones only
    assert group_names(target) == ["Hidden"]
    # unrelated edits never touch the links
    assert patch({"first_name": "T"}).status_code == 200
    assert group_names(target) == ["Hidden"]
    # and a hidden group can't be added
    another = Group.objects.create(name="Secret")
    resp = patch({"groups": [another.pk]})
    assert resp.status_code == 400 and "groups" in resp.json()["errors"]


def test_superusers_see_every_link(groups_setup, admin_client):
    _client, target, *_ = groups_setup
    body = admin_client.get(crud("auth.user", target.pk)).json()
    assert len(body["values"]["groups"]) == 2 and "hidden_links" not in body


def test_hidden_fk_targets_are_restricted_but_round_trip(make_staff, seeded, temp_rule):
    item = OrderItem.objects.select_related("product").first()
    temp_rule(Product, lambda user: ~Q(pk=item.product_id))
    client = Client()
    client.force_login(make_staff("shop.view_orderitem", "shop.change_orderitem"))
    body = client.get(crud("shop.orderitem", item.pk)).json()
    product = body["values"]["product"]
    assert product["restricted"] is True and product["pk"] == item.product_id
    assert item.product.name not in str(body["values"])
    # the form sends the restricted ref back as it was
    resp = client.patch(
        crud("shop.orderitem", item.pk), {"product": product, "quantity": 7}, "application/json"
    )
    assert resp.status_code == 200, resp.content
    item.refresh_from_db()
    assert (item.quantity, item.product_id) == (7, product["pk"])


def test_list_and_drilldown_cells_through_hidden_rows_are_restricted(
    make_staff, seeded, temp_rule, report_api
):
    hidden = Customer.objects.filter(orders__isnull=False).first()
    temp_rule(Customer, lambda user: ~Q(pk=hidden.pk))
    client = Client()
    client.force_login(make_staff("shop.view_order"))
    body = client.get(crud("shop.order"), {"page_size": 500}).json()
    names = [c["name"] for c in body["columns"]]
    cust, city = names.index("customer"), names.index("customer__city__name")
    hidden_orders = set(hidden.orders.values_list("pk", flat=True))
    for row in body["rows"]:
        restricted = row["pk"] in hidden_orders
        assert (row["cells"][cust].get("restricted") is True) == restricted
        assert isinstance(row["cells"][city], dict) == restricted
        if restricted:
            assert hidden.name not in str(row["cells"])
    assert any(r["pk"] in hidden_orders for r in body["rows"])

    spec = {"semantic_model": "order_metrics", "measures": ["orders"]}
    drill = report_api(client, "drilldown", {"spec": spec, "cell": {}, "page_size": 200}).json()
    restricted = {r["pk"] for r in drill["rows"] if r["cells"][cust].get("restricted")}
    assert restricted == hidden_orders & {r["pk"] for r in drill["rows"]}


def test_reverse_relations_in_fields_are_scoped_and_read_only(make_staff, seeded, temp_rule):
    class CustomerWithOrders(ModelOptions):
        fields = ["name", "phone", "city", "created_at", "orders"]

    customer = Customer.objects.annotate().filter(orders__isnull=False).first()
    keep = customer.orders.order_by("pk").first()
    temp_rule(Order, lambda user: Q(pk=keep.pk))
    site.register(Customer, CustomerWithOrders)
    try:
        client = Client()
        client.force_login(make_staff("shop.view_customer", "shop.change_customer"))
        meta = client.get(f"{API}/meta/models/shop/customer").json()
        assert "orders" in meta["form"]["readonly_fields"]
        field = next(f for f in meta["fields"] if f["name"] == "orders")
        assert (field["kind"], field["editable"], field["related_model"]) == (
            "m2m",
            False,
            "shop.order",
        )
        body = client.get(crud("shop.customer", customer.pk)).json()
        assert [o["pk"] for o in body["values"]["orders"]] == [keep.pk]
        if customer.orders.count() > 1:
            assert body["hidden_links"] == {"orders": customer.orders.count() - 1}
        resp = client.patch(
            crud("shop.customer", customer.pk), {"name": "Renamed"}, "application/json"
        )
        assert resp.status_code == 200
        assert (
            client.patch(
                crud("shop.customer", customer.pk), {"orders": []}, "application/json"
            ).status_code
            == 400
        )  # read-only
    finally:
        from shop.gozar import CustomerOptions

        site.register(Customer, CustomerOptions)
