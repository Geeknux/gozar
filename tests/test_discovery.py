"""M1: model discovery registry + relation graph."""

import pytest
from django.test import override_settings

from bi_admin.discovery import discover, get_registry, reset_registry


def test_registry_contains_demo_and_auth_models():
    reg = get_registry()
    for label in ["shop.order", "shop.customer", "shop.city", "shop.product", "shop.orderitem"]:
        assert label in reg
    assert "auth.user" in reg and "auth.group" in reg
    # framework plumbing is hidden by default
    assert "contenttypes.contenttype" not in reg
    assert "sessions.session" not in reg
    assert "auth.permission" not in reg
    assert "bi_admin.changelog" not in reg


def test_field_metadata_from_meta():
    order = get_registry().get("shop.order")
    assert order.verbose_name == "order"
    assert order.verbose_name_plural == "orders"
    assert order.app_verbose_name == "Shop"
    assert order.pk_name == "id"

    status = order.fields["status"]
    assert status.kind == "string"
    assert ("paid", "Paid") in status.choices
    assert status.has_default and not status.required

    amount = order.fields["amount"]
    assert (amount.kind, amount.max_digits, amount.decimal_places) == ("decimal", 16, 0)

    customer = order.fields["customer"]
    assert customer.kind == "fk" and customer.related_model == "shop.customer"
    assert customer.required

    paid_at = order.fields["paid_at"]
    assert paid_at.kind == "datetime" and paid_at.null and not paid_at.required

    assert not order.fields["id"].editable
    assert get_registry().get("auth.user").fields["groups"].kind == "m2m"


def test_relations_both_directions():
    reg = get_registry()
    order = reg.get("shop.order")
    assert order.relations["customer"].kind == "fk"
    assert not order.relations["customer"].many
    assert order.relations["items"].kind == "reverse_fk"
    assert order.relations["items"].many
    city = reg.get("shop.city")
    assert city.relations["customers"].target == "shop.customer"


def test_find_path_prefers_to_one_hops():
    reg = get_registry()
    path = reg.find_path("shop.order", "shop.city")
    assert [s.relation for s in path] == ["customer", "city"]
    assert not any(s.many for s in path)
    assert reg.find_path("shop.city", "shop.city") == []
    item_to_city = reg.find_path("shop.orderitem", "shop.city")
    assert "__".join(s.relation for s in item_to_city) == "order__customer__city"


def test_resolve_lookup_validates_paths():
    reg = get_registry()
    steps, field = reg.resolve_lookup("shop.order", "customer__city__name")
    assert [s.target for s in steps] == ["shop.customer", "shop.city"]
    assert field.name == "name" and field.kind == "string"

    steps, field = reg.resolve_lookup("shop.order", "customer")
    assert field.name == "customer"  # forward FK resolves to the field itself

    with pytest.raises(LookupError):
        reg.resolve_lookup("shop.order", "customer__nope")
    with pytest.raises(LookupError):
        reg.resolve_lookup("shop.order", "amount__city")


def test_lookup_paths_follow_to_one_relations():
    paths = get_registry().lookup_paths("shop.order", max_depth=2)
    assert "customer__city__province" in paths
    assert not any(p.startswith("items__") for p in paths)


def test_graph_edges_only_between_registered_models():
    g = get_registry().graph()
    labels = {n["label"] for n in g["nodes"]}
    assert all(e["source"] in labels and e["target"] in labels for e in g["edges"])
    assert any(e["source"] == "shop.order" and e["target"] == "shop.customer" for e in g["edges"])


@override_settings(GOZAR={"INCLUDE_APPS": ["shop"], "EXCLUDE_MODELS": ["shop.orderitem"]})
def test_include_exclude_settings():
    reset_registry()
    reg = discover()
    assert "shop.order" in reg
    assert "auth.user" not in reg
    assert "shop.orderitem" not in reg


def test_registry_is_kept_per_language():
    from django.utils import translation

    en = get_registry().get("shop.order")
    with translation.override("fa"):
        fa = get_registry().get("shop.order")
        assert (fa.verbose_name, fa.app_verbose_name) == ("سفارش", "فروشگاه")
        assert ("paid", "پرداخت‌شده") in fa.fields["status"].choices
    assert en.verbose_name == "order" and get_registry().get("shop.order") is en
