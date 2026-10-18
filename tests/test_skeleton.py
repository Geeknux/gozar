"""M0 smoke tests: app wiring, demo models, seed command, API health."""

from io import StringIO

import pytest
from django.apps import apps
from django.core.management import call_command
from django.utils import translation

import bi_admin
from shop.models import City, Customer, Order, OrderItem, Product


def seed(**kwargs):
    call_command("seed_demo", customers=20, orders=100, seed=7, stdout=StringIO(), **kwargs)


def test_app_installed():
    assert apps.get_app_config("bi_admin").verbose_name == "Gozar"


def test_demo_models_relations_and_labels():
    assert Order._meta.verbose_name == "order"
    with translation.override("fa"):
        assert str(Order._meta.verbose_name) == "سفارش"
    assert Order._meta.get_field("customer").related_model is Customer
    assert Customer._meta.get_field("city").related_model is City


def test_api_health(client):
    resp = client.get("/gozar/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": bi_admin.__version__}


@pytest.mark.django_db
def test_seed_demo_counts_and_consistency():
    seed()

    assert City.objects.count() == 10
    assert Product.objects.count() == 12
    assert Customer.objects.count() == 20
    assert Order.objects.count() == 100
    assert OrderItem.objects.exists()

    for order in Order.objects.prefetch_related("items"):
        assert order.amount == sum(i.unit_price * i.quantity for i in order.items.all())

    assert not Order.objects.filter(status__in=["pending", "cancelled"], paid_at__isnull=False)
    assert not Order.objects.filter(status__in=["paid", "refunded"], paid_at__isnull=True)


@pytest.mark.django_db
def test_seed_demo_is_deterministic():
    seed()
    first = list(Order.objects.order_by("id").values_list("status", "amount"))
    seed(flush=True)
    second = list(Order.objects.order_by("id").values_list("status", "amount"))
    assert first == second
