"""M6.5: form options — fieldsets, widgets, read-only computed fields, prepopulated
fields, view on site, save buttons, and the configuration checks."""

import pytest
from django.core import checks

from bi_admin.crud import ModelOptions, TabularInline, display, register, site
from shop.models import City, Customer, Order, OrderItem, Product

API = "/gozar/api"
pytestmark = pytest.mark.django_db


def crud(label, suffix=""):
    app, model = label.split(".")
    return f"{API}/crud/{app}/{model}/{suffix}"


@pytest.fixture
def temp_options():
    """Register options for a model for one test only."""
    saved = {}

    def use(model, cls):
        label = model._meta.label_lower
        saved.setdefault(label, site._options.get(label))
        register(model)(cls)

    yield use
    for label, cls in saved.items():
        if cls is None:
            site._options.pop(label, None)
        else:
            site._options[label] = cls
    site.clear_cache()


def test_fieldsets_with_rows_collapse_and_description(admin_client, seeded):
    form = admin_client.get(f"{API}/meta/models/shop/order").json()["form"]
    main, payment = form["fieldsets"]
    assert main["title"] is None and main["rows"] == [
        ["customer"],
        ["status"],
        ["amount", "created_at"],
    ]
    assert payment == {
        "title": "Payment",
        "description": "Set when the payment gateway confirms the order.",
        "classes": ["collapse"],
        "collapse": True,
        "rows": [["paid_at"], ["items_total"]],
    }
    assert form["fields"] == [
        "customer",
        "status",
        "amount",
        "created_at",
        "paid_at",
        "items_total",
    ]
    # fields outside the fieldsets aren't editable through the API
    order = Order.objects.first()
    assert (
        admin_client.patch(
            crud("shop.order", order.pk), {"customer": order.customer_id}, "application/json"
        ).status_code
        == 200
    )


def test_default_fieldset_and_widgets(admin_client, seeded):
    form = admin_client.get(f"{API}/meta/models/shop/customer").json()["form"]
    (only,) = form["fieldsets"]
    assert only["title"] is None and only["rows"] == [[n] for n in form["fields"]]
    assert form["widgets"]["city"] == {"type": "autocomplete"}
    order = admin_client.get(f"{API}/meta/models/shop/order").json()["form"]
    assert order["widgets"]["customer"] == {"type": "raw_id"}
    assert order["widgets"]["status"] == {"type": "radio", "orientation": "horizontal"}
    product = admin_client.get(f"{API}/meta/models/shop/product").json()["form"]
    assert product["widgets"]["description"] == {"type": "textarea"}
    assert product["widgets"]["category"] == {"type": "rich_select"}
    assert product["widgets"]["tags"] == {"type": "dual_list", "orientation": "horizontal"}
    assert product["widgets"]["is_active"] == {"type": "checkbox"}
    assert product["widgets"]["datasheet"] == {"type": "file"}
    assert product["prepopulated"] == {"slug": ["name"]}
    assert product["save_as"] is True and product["save_as_continue"] is True


def test_readonly_computed_fields(admin_client, seeded):
    meta = admin_client.get(f"{API}/meta/models/shop/order").json()
    fields = {f["name"]: f for f in meta["fields"]}
    assert fields["items_total"]["computed"] is True
    assert fields["items_total"]["verbose_name"] == "items total"
    assert "items_total" in meta["form"]["readonly_fields"]
    order = Order.objects.filter(items__isnull=False).first()
    value = admin_client.get(crud("shop.order", order.pk)).json()["values"]["items_total"]
    assert value == str(sum(i.quantity * i.unit_price for i in order.items.all()))
    resp = admin_client.patch(crud("shop.order", order.pk), {"items_total": 5}, "application/json")
    assert resp.status_code == 400


def test_readonly_callables_model_methods_and_admin_display(admin_client, seeded, temp_options):
    from django.contrib import admin

    def doubled(customer):
        return len(customer.name) * 2

    @admin.display(description="Shout", boolean=True)
    def shout(customer):
        return customer.name.isupper()

    class CustomerOptions(ModelOptions):
        fields = ["name", "city", doubled, "__str__", shout]
        readonly_fields = [doubled, "__str__", shout]
        list_display = ["name", shout, "broken"]

        @display(description="Broken")
        def broken(self, obj):
            raise ValueError("boom")

    temp_options(Customer, CustomerOptions)
    meta = admin_client.get(f"{API}/meta/models/shop/customer").json()
    fields = {f["name"]: f for f in meta["fields"]}
    assert fields["shout"]["kind"] == "boolean" and fields["shout"]["verbose_name"] == "Shout"
    customer = Customer.objects.first()
    values = admin_client.get(crud("shop.customer", customer.pk)).json()["values"]
    assert values["doubled"] == len(customer.name) * 2
    assert values["__str__"] == str(customer)
    assert values["shout"] is False
    cols = admin_client.get(f"{API}/meta/models/shop/customer").json()["list"]["columns"]
    assert cols[1]["label"] == "Shout" and cols[1]["boolean"] is True


def test_view_on_site(admin_client, seeded, temp_options):
    product = Product.objects.first()
    data = admin_client.get(crud("shop.product", product.pk)).json()
    assert data["site_url"] == f"/shop/products/{product.pk}/"
    assert admin_client.get(data["site_url"]).status_code == 200  # the demo's public page
    assert "site_url" not in admin_client.get(crud("shop.city", City.objects.first().pk)).json()

    class Off(ModelOptions):
        view_on_site = False

    temp_options(Product, Off)
    assert "site_url" not in admin_client.get(crud("shop.product", product.pk)).json()

    class Custom(ModelOptions):
        view_on_site = staticmethod(lambda obj: f"https://shop.example.com/p/{obj.pk}")

    temp_options(Product, Custom)
    data = admin_client.get(crud("shop.product", product.pk)).json()
    assert data["site_url"] == f"https://shop.example.com/p/{product.pk}"


def test_options_checks_report_mistakes(temp_options):
    class Bad(ModelOptions):
        list_display = ["name", "price"]
        list_display_links = ["name"]
        list_editable = ["name", "category"]  # name is the link; category isn't listed
        date_hierarchy = "price"
        raw_id_fields = ["nope"]
        filter_horizontal = ["category"]
        widgets = {"name": "fancy"}
        prepopulated_fields = {"slug": ["missing"]}

    class BadInline(TabularInline):
        model = OrderItem
        autocomplete_fields = ["quantity"]

    class WithInline(ModelOptions):
        inlines = [BadInline]

    temp_options(Product, Bad)
    temp_options(Order, WithInline)
    from bi_admin.checks import check_model_options

    messages = [e.msg for e in check_model_options() if e.id == "bi_admin.E005"]
    text = "\n".join(messages)
    for needle in (
        "list_editable: 'name' is also in list_display_links",
        "list_editable: 'category' is not in list_display",
        "date_hierarchy: 'price' is not a date",
        "raw_id_fields: 'nope'",
        "'category' is not a many-to-many field",
        "unknown widget 'fancy'",
        "prepopulated_fields: unknown field 'missing'",
        "'quantity' is not a relation field",
    ):
        assert needle in text, needle

    class NoFk(TabularInline):
        model = City

    class Broken(ModelOptions):
        inlines = [NoFk]

    temp_options(Customer, Broken)
    errors = [e for e in check_model_options() if e.obj == "shop.customer"]
    assert errors and "has no ForeignKey" in errors[0].msg


def test_demo_options_pass_the_checks():
    assert [e for e in checks.run_checks() if e.id == "bi_admin.E005"] == []


def test_list_display_links_skip_editable_columns(admin_client, seeded, temp_options):
    class P(ModelOptions):
        list_display = ["price", "name"]
        list_editable = ["price"]

    temp_options(Product, P)
    cols = admin_client.get(crud("shop.product")).json()["columns"]
    assert [c["is_link"] for c in cols] == [False, True]


def test_raw_id_lookup_by_pk_is_scoped(admin_client, regional, seeded):
    from django.test import Client

    outside = Customer.objects.exclude(city__name_en__in=["Tehran", "Karaj", "Qom"]).first()
    url = crud("shop.order", "autocomplete")
    hit = admin_client.get(url, {"field": "customer", "pk": outside.pk}).json()["results"]
    assert [r["pk"] for r in hit] == [outside.pk]
    client = Client()
    client.force_login(regional())
    assert client.get(url, {"field": "customer", "pk": outside.pk}).json()["results"] == []
    assert client.get(url, {"field": "customer", "pk": "abc"}).json()["results"] == []
