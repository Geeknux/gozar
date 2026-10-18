"""M2: the declarative semantic layer, its catalog and autodiscovery."""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Count, F, Sum
from django.db.models.functions import Upper
from django.utils.translation import gettext_lazy

from bi_admin.checks import check_semantic_models
from bi_admin.semantic import Dimension, Measure, Semantic, catalog, register, semantic_site
from shop.models import Customer, Order


@pytest.fixture
def scratch():
    """Register throw-away semantic models; unregistered afterwards."""
    yield register
    for name in semantic_site.names():
        if name != "order_metrics":
            semantic_site.unregister(name)


def test_order_metrics_is_autodiscovered_from_gozar_py():
    assert "order_metrics" in semantic_site
    sem = semantic_site.get("order_metrics")
    assert sem.model is Order and sem.model_label == "shop.order"
    assert sem.time_dimension == "created_at"
    assert set(sem.measures) == {"revenue", "orders", "paid_orders", "aov", "customers"}
    assert {"created_at", "paid_at", "city", "province", "status"} <= set(sem.dimensions)
    assert sem.label == {"fa": "سفارش‌ها", "en": "Orders"}


def test_dimension_binding_resolves_paths_and_types():
    sem = semantic_site.get("order_metrics")
    city = sem.dimensions["city"]
    assert city.path == "customer__city__name"
    assert [s.relation for s in city.steps] == ["customer", "city"]
    assert city.field.name == "name" and not city.fans_out
    assert city.label == {"fa": "شهر", "en": "City"}
    assert city.synonyms["fa"] == ["محل", "شهرستان"]
    created = sem.dimensions["created_at"]
    assert created.type == "time" and not created.is_date
    status = sem.dimensions["status"]
    assert ("paid", {"fa": "پرداخت‌شده", "en": "Paid"}) in status.choices
    assert sem.dimensions["amount"].type == "number"


def test_measure_metadata():
    revenue = semantic_site.get("order_metrics").measures["revenue"]
    assert revenue.format == "currency"
    assert revenue.label == {"fa": "درآمد", "en": "Revenue"}
    assert "گردش مالی" in revenue.synonyms["fa"] and revenue.synonyms["en"] == ["sales"]
    assert revenue.description["en"] == "Sum of amounts of paid orders"


def test_defaults_are_derived_from_meta(scratch):
    @scratch
    class CustomerStats(Semantic):
        model = Customer
        dimensions = {
            "city": Dimension("city__name"),
            "joined": Dimension("created_at"),
            "city_fk": Dimension("city"),
            "order_status": Dimension("orders__status"),
            "upper_name": Dimension(Upper("name"), type="category"),
        }
        measures = {"customers": Measure(Count("id"))}

    sem = semantic_site.get("customer_stats")
    assert sem.label == {"fa": "مشتریان", "en": "Customers"}  # verbose_name_plural, both langs
    assert sem.time_dimension == "joined"  # first time dimension
    city = sem.dimensions["city"]
    assert city.type == "category"
    assert city.label == {"fa": "شهر / نام", "en": "City / Name"}
    assert sem.dimensions["joined"].type == "time"
    assert sem.dimensions["city_fk"].type == "category"
    assert sem.dimensions["city_fk"].choices is None
    assert sem.dimensions["order_status"].fans_out  # crosses a reverse FK
    assert sem.dimensions["upper_name"].path is None
    assert sem.measures["customers"].label == {"fa": "Customers", "en": "Customers"}


def test_string_labels_are_source_text_through_gettext(scratch):
    @scratch
    class Labelled(Semantic):
        model = Order
        name = "labelled"
        label = gettext_lazy("Shop")  # translated by the shop's catalog
        description = "Plain text"  # no translation: same in both languages
        synonyms = ["purchase"]
        dimensions = {"s": Dimension("status", label={"en": "State"})}
        measures = {"n": Measure(Count("id"), label={"fa": "تعداد"})}

    sem = semantic_site.get("labelled")
    assert sem.label == {"fa": "فروشگاه", "en": "Shop"}
    assert sem.description == {"fa": "Plain text", "en": "Plain text"}
    assert sem.synonyms == {"fa": ["purchase"], "en": ["purchase"]}
    assert sem.dimensions["s"].label == {"fa": "وضعیت", "en": "State"}
    assert sem.measures["n"].label == {"fa": "تعداد", "en": "N"}


@pytest.mark.parametrize(
    "dimensions, measures, match",
    [
        ({"x": Dimension("customer__nope")}, {"n": Measure(Count("id"))}, "Unknown field"),
        ({"x": Dimension("status", type="time")}, {"n": Measure(Count("id"))}, "not a date"),
        ({"x": Dimension(Upper("status"))}, {"n": Measure(Count("id"))}, "needs a type"),
        ({}, {"n": Measure(Sum("nope"))}, "nope"),
        ({}, {}, "at least one measure"),
        ({"n": Dimension("status")}, {"n": Measure(Count("id"))}, "both a dimension"),
    ],
)
def test_misconfigured_semantic_models(scratch, dimensions, measures, match):
    @scratch
    class Broken(Semantic):
        model = Order

    Broken.dimensions = dimensions
    Broken.measures = measures
    with pytest.raises(ImproperlyConfigured, match=match):
        semantic_site.get("broken")
    errors = check_semantic_models()
    assert [e.id for e in errors] == ["bi_admin.E001"] and errors[0].obj == "broken"


def test_declaration_errors_are_immediate():
    with pytest.raises(ImproperlyConfigured):
        Measure(F("amount"))  # not an aggregate
    with pytest.raises(ImproperlyConfigured):
        Measure(Count("id"), format="money")
    with pytest.raises(ImproperlyConfigured):
        Dimension("status", type="colour")
    with pytest.raises(ImproperlyConfigured):
        Dimension(42)
    with pytest.raises(ImproperlyConfigured):
        register(object)


def test_bad_time_dimension_and_duplicate_names(scratch):
    @scratch
    class BadTime(Semantic):
        model = Order
        time_dimension = "status"
        dimensions = {"status": Dimension("status")}
        measures = {"n": Measure(Count("id"))}

    with pytest.raises(ImproperlyConfigured, match="not a time dimension"):
        semantic_site.get("bad_time")

    class OrderMetrics(Semantic):  # same name as the demo's, different class
        model = Order
        measures = {"n": Measure(Count("id"))}

    with pytest.raises(ImproperlyConfigured, match="already registered"):
        register(OrderMetrics)


def test_hidden_items_are_left_out_of_the_catalog(scratch):
    @scratch
    class Hidden(Semantic):
        model = Order
        dimensions = {"status": Dimension("status"), "secret": Dimension("amount", hidden=True)}
        measures = {"n": Measure(Count("id")), "total": Measure(Sum("amount"), hidden=True)}

    entry = semantic_site.get("hidden").catalog("en")
    assert [d["key"] for d in entry["dimensions"]] == ["status"]
    assert [m["key"] for m in entry["measures"]] == ["n"]


def test_catalog_languages():
    both = next(c for c in catalog() if c["name"] == "order_metrics")
    assert both["label"] == {"fa": "سفارش‌ها", "en": "Orders"}
    assert both["time_dimension"] == "created_at"
    fa = next(c for c in catalog("fa") if c["name"] == "order_metrics")
    en = next(c for c in catalog("en") if c["name"] == "order_metrics")
    assert fa["label"] == "سفارش‌ها" and en["label"] == "Orders"
    revenue_fa = next(m for m in fa["measures"] if m["key"] == "revenue")
    assert revenue_fa == {
        "key": "revenue",
        "format": "currency",
        "label": "درآمد",
        "synonyms": ["فروش", "گردش مالی", "دریافتی"],
        "description": "جمع مبلغ سفارش‌های پرداخت‌شده",
    }
    created_en = next(d for d in en["dimensions"] if d["key"] == "created_at")
    assert created_en["type"] == "time" and created_en["label"] == "Order date"
    assert created_en["grains"] == ["day", "week", "month", "season", "year"]
    status_en = next(d for d in en["dimensions"] if d["key"] == "status")
    assert {"value": "paid", "label": "Paid"} in status_en["choices"]
    status_fa = next(d for d in fa["dimensions"] if d["key"] == "status")
    assert {"value": "paid", "label": "پرداخت‌شده"} in status_fa["choices"]


@pytest.mark.django_db
def test_catalog_is_filtered_by_permission(rf, make_staff, admin_user):
    request = rf.get("/")
    request.user = make_staff("shop.view_customer")
    assert catalog(request=request) == []
    request.user = make_staff("shop.view_order", username="viewer")
    assert [c["name"] for c in catalog(request=request)] == ["order_metrics"]
    request.user = admin_user
    assert [c["name"] for c in catalog(request=request)] == ["order_metrics"]
