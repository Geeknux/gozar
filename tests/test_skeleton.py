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


def snapshot():
    customers = list(Customer.objects.order_by("id").values_list("name", "phone", "city__name_en"))
    orders = list(Order.objects.order_by("id").values_list("status", "amount", "customer__phone"))
    return customers, orders


@pytest.mark.django_db
@pytest.mark.parametrize("locale", ["en", "fa"])
def test_seed_demo_is_deterministic(locale):
    seed(locale=locale)
    first = snapshot()
    seed(flush=True, locale=locale)
    assert snapshot() == first


@pytest.mark.django_db
def test_seed_demo_english_by_default():
    seed()
    tehran = City.objects.get(name="Tehran")
    assert (tehran.name_en, tehran.province) == ("Tehran", "Tehran")
    assert City.objects.get(name="Mashhad").province == "Razavi Khorasan"
    assert Product.objects.filter(name="Saffron 5 g", category="grocery").exists()
    for text in [
        *City.objects.values_list("name", flat=True),
        *City.objects.values_list("province", flat=True),
        *Product.objects.values_list("name", flat=True),
        *Customer.objects.values_list("name", flat=True),
    ]:
        assert text.isascii(), text


@pytest.mark.django_db
def test_seed_demo_persian_locale():
    seed(locale="fa")
    tehran = City.objects.get(name="تهران")
    assert (tehran.name_en, tehran.province) == ("Tehran", "تهران")  # both name fields filled
    assert City.objects.get(name="مشهد").province == "خراسان رضوی"
    assert Product.objects.filter(name="زعفران ۵ گرم").exists()
    assert all(not name.isascii() for name in Customer.objects.values_list("name", flat=True))


@pytest.mark.django_db
def test_seed_locales_differ_only_in_names():
    """Same seed → same draws: the fa data set is the en one with Persian names."""
    seed()
    en_customers, en_orders = snapshot()
    seed(flush=True, locale="fa")
    fa_customers, fa_orders = snapshot()
    assert en_orders == fa_orders
    assert [(p, c) for _, p, c in en_customers] == [(p, c) for _, p, c in fa_customers]
    assert [n for n, _, _ in en_customers] != [n for n, _, _ in fa_customers]


@pytest.mark.django_db
def test_seed_demo_rejects_unknown_locale():
    from django.core.management import CommandError

    with pytest.raises(CommandError):
        seed(locale="de")


@pytest.mark.django_db
def test_seed_demo_catalogue_media_and_tags():
    from django.core.files.storage import default_storage

    from shop.models import Product, ProductImage, Tag

    seed()
    assert Tag.objects.count() == 4
    laptop = Product.objects.get(name="Laptop")
    assert laptop.slug == "laptop" and laptop.description
    assert default_storage.exists(laptop.datasheet.name)
    assert laptop.datasheet.read().startswith(b"%PDF-1.4")
    images = ProductImage.objects.all()
    assert images.count() == 12 and all(default_storage.exists(i.image.name) for i in images)
    assert set(Product.objects.get(name="Saffron 5 g").tags.values_list("name", flat=True)) == {
        "Bestseller",
        "Gift idea",
        "Made in Iran",
    }
    seed()  # idempotent: pictures, datasheet and tags aren't duplicated
    assert ProductImage.objects.count() == 12 and Tag.objects.count() == 4
