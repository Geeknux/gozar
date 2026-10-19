"""M4: row-level rules — a user never sees or touches rows outside their scope.

The demo rules (shop/gozar.py) limit staff in "Sales: Tehran region" to Tehran,
Karaj and Qom, on every path: report run, drill-down, export, value lookups, CRUD
list/filters/detail/update/create, autocomplete, actions, delete plans, history
and background jobs."""

import csv
import io

import pytest
from conftest import TEHRAN_REGION
from django.db.models import Count, Q, Sum

from bi_admin.access import rule
from bi_admin.access.rules import registry, semantic_queryset
from bi_admin.crud import site
from bi_admin.engine import run
from bi_admin.semantic import Dimension, Measure, Semantic, semantic_site
from shop.models import City, Customer, Order, OrderItem, Product

pytestmark = pytest.mark.django_db

API = "/gozar/api"
SPEC = {"semantic_model": "order_metrics", "measures": ["orders", "revenue"]}
IN_REGION = Q(customer__city__name_en__in=TEHRAN_REGION)


def crud(label, suffix=""):
    app, model = label.split(".")
    return f"{API}/crud/{app}/{model}/{suffix}"


@pytest.fixture
def scoped(client, regional, seeded):
    client.force_login(regional())
    return client


@pytest.fixture
def temp_rule():
    """Register rules for one test only."""
    added = []

    def add(target, predicate):
        rule(target, predicate)
        added.append((target, predicate))

    yield add
    for target, predicate in added:
        registry.remove(target, predicate)


def visible_orders():
    return Order.objects.filter(IN_REGION)


def outside_order():
    return Order.objects.exclude(IN_REGION).first()


def test_seed_has_rows_on_both_sides(seeded):
    assert visible_orders().exists() and Order.objects.exclude(IN_REGION).exists()


# --- reports -------------------------------------------------------------------------


def test_report_run_only_counts_rows_in_scope(scoped, report_api):
    body = report_api(scoped, "run", {**SPEC, "dimensions": ["city"]}).json()
    assert {r["city"] for r in body["rows"]} <= set(TEHRAN_REGION)
    expected = visible_orders().values("customer__city__name").annotate(n=Count("id"))
    assert {r["city"]: r["orders"] for r in body["rows"]} == {
        e["customer__city__name"]: e["n"] for e in expected
    }
    total = report_api(scoped, "run", SPEC).json()["rows"][0]
    paid = visible_orders().filter(status="paid").aggregate(s=Sum("amount"))["s"]
    assert total == {"orders": visible_orders().count(), "revenue": int(paid)}


def test_filtering_on_an_outside_value_finds_nothing(scoped, report_api):
    spec = {**SPEC, "filters": [{"field": "city", "op": "eq", "value": "Shiraz"}]}
    assert report_api(scoped, "run", spec).json()["rows"][0]["orders"] == 0


def test_validate_works_for_scoped_users(scoped, report_api):
    assert report_api(scoped, "validate", {**SPEC, "dimensions": ["city"]}).json()["valid"]


def test_drilldown_only_lists_rows_in_scope(scoped, report_api):
    body = report_api(scoped, "drilldown", {"spec": SPEC, "cell": {}, "page_size": 200}).json()
    assert body["count"] == visible_orders().count()
    assert {r["pk"] for r in body["rows"]} == set(visible_orders().values_list("pk", flat=True))
    spec = {**SPEC, "dimensions": ["city"]}
    outside = report_api(scoped, "drilldown", {"spec": spec, "cell": {"city": "Shiraz"}})
    assert outside.status_code == 200 and outside.json()["count"] == 0


def test_export_only_contains_rows_in_scope(scoped, report_api):
    resp = report_api(scoped, "export", {**SPEC, "dimensions": ["city"]}, format="csv")
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8")[1:])))
    assert {r[0] for r in rows[1:]} <= set(TEHRAN_REGION)
    assert sum(int(r[1]) for r in rows[1:]) == visible_orders().count()


def test_value_lookups_only_offer_values_in_scope(scoped):
    get = lambda **p: scoped.get(f"{API}/reports/values", p).json()["values"]  # noqa: E731
    cities = {v["value"] for v in get(semantic_model="order_metrics", dimension="city")}
    assert cities and cities <= set(TEHRAN_REGION)
    customers = {v["value"] for v in get(semantic_model="order_metrics", dimension="customer")}
    expected = set(visible_orders().values_list("customer__name", flat=True))
    assert customers == expected


def test_relation_value_lookups_use_the_target_models_rules(scoped, monkeypatch):
    class ByCustomer(Semantic):
        model = Order
        dimensions = {"customer": Dimension("customer")}
        measures = {"n": Measure(Count("id"))}

    semantic_site.register(ByCustomer)
    try:
        values = scoped.get(
            f"{API}/reports/values", {"semantic_model": "by_customer", "dimension": "customer"}
        ).json()["values"]
    finally:
        semantic_site.unregister("by_customer")
    allowed = set(
        Customer.objects.filter(city__name_en__in=TEHRAN_REGION).values_list("pk", flat=True)
    )
    assert {v["value"] for v in values} <= allowed


def test_semantic_queryset_applies_model_and_semantic_rules(regional, seeded, temp_rule, rf):
    user = regional()
    request = rf.get("/")
    request.user = user
    sem = semantic_site.get("order_metrics")
    assert semantic_queryset(sem, request).count() == visible_orders().count()
    temp_rule(type(sem), lambda user: Q(status="paid"))
    request = rf.get("/")  # rules are memoised per request
    request.user = user
    assert semantic_queryset(sem, request).count() == visible_orders().filter(status="paid").count()


def test_semantic_rules_do_not_restrict_crud(client, make_staff, seeded, temp_rule):
    temp_rule(type(semantic_site.get("order_metrics")), lambda user: Q(status="paid"))
    client.force_login(make_staff("shop.view_order"))
    assert client.get(crud("shop.order")).json()["count"] == Order.objects.count()
    paid = Order.objects.filter(status="paid").count()
    body = client.post(f"{API}/reports/run", SPEC, "application/json").json()
    assert body["rows"][0]["orders"] == paid


def test_run_without_a_request_is_unrestricted(seeded):
    assert run(SPEC).rows[0]["orders"] == Order.objects.count()


# --- CRUD ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, q",
    [
        ("shop.order", IN_REGION),
        ("shop.customer", Q(city__name_en__in=TEHRAN_REGION)),
        ("shop.city", Q(name_en__in=TEHRAN_REGION)),
        ("shop.orderitem", Q(order__customer__city__name_en__in=TEHRAN_REGION)),
        ("shop.product", Q()),  # no rule
    ],
)
def test_crud_list_is_scoped(scoped, label, q):
    from django.apps import apps

    model = apps.get_model(label)
    body = scoped.get(crud(label), {"page_size": 500}).json()
    assert body["count"] == body["total"] == model.objects.filter(q).count()
    assert {r["pk"] for r in body["rows"]} == set(
        model.objects.filter(q).values_list("pk", flat=True)
    )


def test_crud_search_and_filters_stay_in_scope(scoped):
    shiraz = City.objects.get(name_en="Shiraz")
    body = scoped.get(crud("shop.order"), {"customer__city": shiraz.pk}).json()
    assert body["count"] == 0
    total = scoped.get(crud("shop.order"), {"q": "a"}).json()["total"]
    assert total == visible_orders().count()


def test_filter_choices_only_list_rows_in_scope(scoped):
    meta = scoped.get(f"{API}/meta/models/shop/order").json()
    city_filter = next(f for f in meta["list"]["filters"] if f["name"] == "customer__city")
    labels = {c["label"] for c in city_filter["choices"]}
    assert labels == set(TEHRAN_REGION)


def test_detail_update_history_and_delete_plan_404_outside_scope(client, regional, seeded):
    scoped = client
    scoped.force_login(regional("Sales: Tehran region", *SHOP_CHANGE, "shop.delete_order"))
    order = outside_order()
    assert scoped.get(crud("shop.order", order.pk)).status_code == 404
    patch = scoped.patch(crud("shop.order", order.pk), {"amount": 1}, "application/json")
    assert patch.status_code == 404
    assert scoped.get(crud("shop.order", f"{order.pk}/history")).status_code == 404
    assert scoped.get(crud("shop.order", f"{order.pk}/delete-plan")).status_code == 404
    assert scoped.delete(crud("shop.order", order.pk)).status_code == 404
    assert Order.objects.filter(pk=order.pk).exists()
    inside = visible_orders().first()
    assert scoped.get(crud("shop.order", inside.pk)).status_code == 200


def test_relation_fields_only_accept_rows_in_scope(client, regional, seeded):
    client.force_login(regional("Sales: Tehran region", *SHOP_CHANGE))
    order = visible_orders().first()
    stranger = Customer.objects.exclude(city__name_en__in=TEHRAN_REGION).first()
    resp = client.patch(crud("shop.order", order.pk), {"customer": stranger.pk}, "application/json")
    assert resp.status_code == 400 and "customer" in resp.json()["errors"]
    order.refresh_from_db()
    assert order.customer_id != stranger.pk
    shiraz = City.objects.get(name_en="Shiraz")
    new = {"name": "Out of region", "city": shiraz.pk, "created_at": "2026-01-01T10:00"}
    resp = client.post(crud("shop.customer"), new, "application/json")
    assert resp.status_code == 400 and "city" in resp.json()["errors"]
    assert not Customer.objects.filter(name="Out of region").exists()
    # an in-scope edit of other fields keeps working
    ok = client.patch(crud("shop.order", order.pk), {"amount": 5}, "application/json")
    assert ok.status_code == 200


def test_existing_hidden_links_survive_unrelated_edits(client, regional, seeded, temp_rule):
    """A row may already point at a hidden row; editing other fields keeps the link."""
    item = OrderItem.objects.filter(order__in=visible_orders()).first()
    temp_rule(Product, lambda user: Q(pk__in=[]) if user.groups.exists() else True)
    client.force_login(regional("Sales: Tehran region", *SHOP_CHANGE))
    resp = client.patch(crud("shop.orderitem", item.pk), {"quantity": 9}, "application/json")
    assert resp.status_code == 200, resp.content
    item.refresh_from_db()
    assert item.quantity == 9
    other = Product.objects.exclude(pk=item.product_id).first()
    resp = client.patch(crud("shop.orderitem", item.pk), {"product": other.pk}, "application/json")
    assert resp.status_code == 400


def test_saving_a_row_out_of_scope_is_refused_and_rolled_back(client, regional, seeded, temp_rule):
    temp_rule(Product, lambda user: Q(category="books"))
    client.force_login(regional("Sales: Tehran region", "shop.add_product", "shop.change_product"))
    new = {"name": "Gadget", "category": "electronics", "price": "10"}
    resp = client.post(crud("shop.product"), new, "application/json")
    assert resp.status_code == 403 and resp.json()["code"] == "out_of_scope"
    assert not Product.objects.filter(name="Gadget").exists()
    book = Product.objects.filter(category="books").first()
    resp = client.patch(crud("shop.product", book.pk), {"category": "home"}, "application/json")
    assert resp.status_code == 403
    book.refresh_from_db()
    assert book.category == "books"
    ok = client.post(crud("shop.product"), {**new, "category": "books"}, "application/json")
    assert ok.status_code == 201


def test_autocomplete_only_offers_rows_in_scope(scoped):
    customers = scoped.get(crud("shop.order", "autocomplete"), {"field": "customer"}).json()
    allowed = set(
        Customer.objects.filter(city__name_en__in=TEHRAN_REGION).values_list("pk", flat=True)
    )
    assert customers["results"] and {r["pk"] for r in customers["results"]} <= allowed
    cities = scoped.get(crud("shop.order", "autocomplete"), {"field": "customer__city"}).json()
    assert {r["label"] for r in cities["results"]} == set(TEHRAN_REGION)


def test_actions_only_touch_rows_in_scope(client, regional, seeded):
    client.force_login(regional("Sales: Tehran region", *SHOP_CHANGE, *SHOP_DELETE))
    inside = visible_orders().filter(status="pending").first()
    outside = Order.objects.exclude(IN_REGION).filter(status="pending").first()
    resp = client.post(
        crud("shop.order", "actions/mark_cancelled"),
        {"pks": [inside.pk, outside.pk]},
        "application/json",
    )
    assert resp.json()["updated"] == 1
    inside.refresh_from_db()
    outside.refresh_from_db()
    assert (inside.status, outside.status) == ("cancelled", "pending")

    plan = client.post(
        crud("shop.order", "delete-plan"), {"pks": [outside.pk]}, "application/json"
    ).json()
    assert plan["summary"] == [] and plan["deletable"]
    resp = client.post(
        crud("shop.order", "actions/delete_selected"),
        {"pks": [inside.pk, outside.pk]},
        "application/json",
    )
    assert resp.json()["deleted"] == 1
    assert not Order.objects.filter(pk=inside.pk).exists()
    assert Order.objects.filter(pk=outside.pk).exists()


def test_delete_refused_when_it_would_cascade_out_of_scope(client, regional, seeded, temp_rule):
    order = visible_orders().annotate(n=Count("items")).filter(n__gte=2).first()
    keep = order.items.order_by("pk").first()
    temp_rule(OrderItem, lambda user: ~Q(pk=keep.pk))
    client.force_login(regional("Sales: Tehran region", *SHOP_CHANGE, *SHOP_DELETE))
    plan = client.get(crud("shop.order", f"{order.pk}/delete-plan")).json()
    assert plan["deletable"] is False and plan["perms_needed"] == []
    assert plan["out_of_scope"] == [
        {"model": "shop.orderitem", "verbose_name": "order items", "count": 1}
    ]
    assert client.delete(crud("shop.order", order.pk)).status_code == 409
    assert Order.objects.filter(pk=order.pk).exists()


def test_protected_samples_never_name_hidden_rows(client, regional, seeded, temp_rule):
    city = City.objects.get(name_en="Tehran")
    visible = city.customers.order_by("pk").first()
    temp_rule(Customer, lambda user: Q(pk=visible.pk))
    client.force_login(regional("Sales: Tehran region", "shop.view_city", "shop.delete_city"))
    plan = client.get(crud("shop.city", f"{city.pk}/delete-plan")).json()
    (protected,) = [p for p in plan["protected"] if p["model"] == "shop.customer"]
    assert protected["count"] == city.customers.count() > 1
    assert [s["pk"] for s in protected["sample"]] == [visible.pk]


# --- rule semantics ------------------------------------------------------------------


def test_superusers_bypass_rules_unless_configured(client, admin_user, seeded, settings):
    from django.contrib.auth.models import Group

    admin_user.groups.add(Group.objects.get(name="Sales: Tehran region"))
    client.force_login(admin_user)
    assert client.get(crud("shop.order")).json()["count"] == Order.objects.count()
    settings.GOZAR = {**settings.GOZAR, "ROW_RULES_BYPASS_SUPERUSER": False}
    assert client.get(crud("shop.order")).json()["count"] == visible_orders().count()
    body = client.post(f"{API}/reports/run", SPEC, "application/json").json()
    assert body["rows"][0]["orders"] == visible_orders().count()


def test_users_outside_any_region_are_unrestricted(client, make_staff, seeded):
    client.force_login(make_staff(*SHOP_VIEW_ALL))
    assert client.get(crud("shop.order")).json()["count"] == Order.objects.count()


@pytest.mark.parametrize(
    "predicate, expected",
    [
        (lambda user: False, 0),
        (lambda user: True, None),
        (lambda user: None, None),
        (lambda user: 1 / 0, 0),  # a failing rule denies everything (fail closed)
        (lambda user: "paid", 0),  # so does a rule returning nonsense
    ],
)
def test_rule_return_values(client, make_staff, seeded, temp_rule, predicate, expected):
    temp_rule(Order, predicate)
    client.force_login(make_staff("shop.view_order"))
    count = client.get(crud("shop.order")).json()["count"]
    assert count == (Order.objects.count() if expected is None else expected)
    body = client.post(f"{API}/reports/run", SPEC, "application/json").json()
    assert body["rows"][0]["orders"] == count


def test_rules_are_anded_and_decorator_form(client, make_staff, seeded, temp_rule):
    temp_rule(Order, lambda user: Q(status="paid"))
    temp_rule("shop.order", lambda user: Q(amount__gte=1_000_000))

    @rule(Order)
    def big_ones(user):
        return Q(amount__lte=50_000_000)

    try:
        client.force_login(make_staff("shop.view_order"))
        expected = Order.objects.filter(
            status="paid", amount__gte=1_000_000, amount__lte=50_000_000
        ).count()
        assert client.get(crud("shop.order")).json()["count"] == expected
    finally:
        registry.remove(Order, big_ones)


def test_model_options_row_rules(client, make_staff, seeded, monkeypatch):
    options = site.options_for("shop.product")
    monkeypatch.setattr(options, "row_rules", [lambda user: Q(category="books")], raising=False)
    client.force_login(make_staff("shop.view_product"))
    count = client.get(crud("shop.product")).json()["count"]
    assert count == Product.objects.filter(category="books").count()


def test_rule_targets_are_validated():
    with pytest.raises(TypeError):
        rule(42, lambda user: True)
    with pytest.raises(TypeError):
        rule(Order, "not callable")


# --- background jobs and cache keep the scope -----------------------------------------


def test_jobs_run_with_the_submitters_scope(scoped, report_api, settings):
    settings.GOZAR = {**settings.GOZAR, "JOB_BACKEND": "bi_admin.execution.jobs.ImmediateBackend"}
    resp = report_api(scoped, "run", SPEC, mode="async", cache="false")
    assert resp.status_code == 202
    job = resp.json()["job"]
    assert job["status"] == "done"
    result = scoped.get(f"{API}/reports/jobs/{job['id']}/result").json()
    assert result["rows"][0]["orders"] == visible_orders().count()


def test_cached_results_never_cross_scopes(client, regional, make_staff, seeded, report_api):
    client.force_login(regional())
    first = report_api(client, "run", SPEC).json()
    assert first["rows"][0]["orders"] == visible_orders().count()
    client.force_login(regional("Sales: East region", username="east"))
    east = report_api(client, "run", SPEC).json()
    assert east["cache"]["hit"] is False
    expected = Order.objects.filter(customer__city__name_en__in=["Mashhad", "Kerman"]).count()
    assert east["rows"][0]["orders"] == expected
    client.force_login(make_staff("shop.view_order", username="everyone"))
    everyone = report_api(client, "run", SPEC).json()
    assert everyone["cache"]["hit"] is False
    assert everyone["rows"][0]["orders"] == Order.objects.count()


SHOP_VIEW_ALL = [f"shop.view_{m}" for m in ("city", "customer", "product", "order", "orderitem")]
SHOP_CHANGE = [*SHOP_VIEW_ALL, "shop.change_order", "shop.change_orderitem", "shop.add_customer"]
SHOP_DELETE = ["shop.delete_order", "shop.delete_orderitem"]
