"""Gozar options for the demo shop (auto-discovered, like admin.py)."""

from django.db.models import Avg, Count, Q, Sum
from django.utils.translation import gettext_lazy as _

from bi_admin import semantic
from bi_admin.access import rule
from bi_admin.crud import ModelOptions, StackedInline, TabularInline, action, display, register
from shop.models import City, Customer, Order, OrderItem, Product, ProductImage

# --- row-level rules -----------------------------------------------------------------
# Staff in a sales-region group see only their region's cities — and so only those
# cities' customers, orders and order items, in CRUD and in every report. Users in
# no region group are unrestricted; superusers bypass rules. `seed_demo` creates the
# groups (and, with --users, demo accounts in them).

REGIONS = {
    "Sales: Tehran region": ["Tehran", "Karaj", "Qom"],
    "Sales: East region": ["Mashhad", "Kerman"],
}


def region_cities(user) -> list[str] | None:
    """English names of the cities ``user`` may see; ``None``: every city."""
    groups = set(user.groups.values_list("name", flat=True)) & set(REGIONS)
    if not groups:
        return None
    return sorted({city for group in groups for city in REGIONS[group]})


def in_region(city_path: str):
    """A row rule limiting rows to the user's region, via ``city_path``."""
    prefix = f"{city_path}__" if city_path else ""

    def row_rule(user):
        cities = region_cities(user)
        return True if cities is None else Q(**{f"{prefix}name_en__in": cities})

    return row_rule


# The function API; the same rules could be ModelOptions.row_rules (as below).
rule(City, in_region(""))
rule(OrderItem, in_region("order__customer__city"))


class OrderItemInline(TabularInline):
    model = OrderItem
    extra = 1
    autocomplete_fields = ["product"]
    readonly_fields = ["line_total"]

    @display(description=_("line total"))
    def line_total(self, item):
        return item.quantity * item.unit_price


@register(Order)
class OrderOptions(ModelOptions):
    list_display = [
        "__str__",
        "customer",
        "customer__city__name",
        "status",
        "amount",
        "is_paid",
        "created_at",
    ]
    list_editable = ["status"]
    search_fields = ["customer__name", "customer__phone"]
    search_help_text = _("Search by customer name or phone number.")
    list_filter = ["status", "created_at", "paid_at", "customer__city"]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]
    raw_id_fields = ["customer"]
    radio_fields = {"status": "horizontal"}
    fieldsets = [
        (None, {"fields": ["customer", "status", ("amount", "created_at")]}),
        (
            _("Payment"),
            {
                "fields": ["paid_at", "items_total"],
                "classes": ["collapse"],
                "description": _("Set when the payment gateway confirms the order."),
            },
        ),
    ]
    readonly_fields = ["paid_at", "items_total"]
    inlines = [OrderItemInline]
    save_on_top = True
    actions = ["mark_cancelled"]
    row_rules = [in_region("customer__city")]

    @display(description=_("paid"), boolean=True, ordering="paid_at")
    def is_paid(self, order):
        return order.status == Order.Status.PAID

    @display(description=_("items total"))
    def items_total(self, order):
        return sum(i.quantity * i.unit_price for i in order.items.all())

    @action(_("Cancel selected orders"), permission="change")
    def mark_cancelled(self, request, queryset):
        updated = queryset.filter(status=Order.Status.PENDING).update(status=Order.Status.CANCELLED)
        return {"updated": updated}


class ProductImageInline(StackedInline):
    model = ProductImage
    extra = 1
    max_num = 6
    fields = ["image", "caption", "position"]


@register(Product)
class ProductOptions(ModelOptions):
    list_display = ["name", "category", "price", "is_active", "image_count"]
    list_display_links = ["name"]
    list_editable = ["price", "is_active"]
    list_filter = ["category", "is_active", "tags"]
    search_fields = ["name", "slug", "description"]
    sortable_by = ["name", "category", "price"]
    fieldsets = [
        (None, {"fields": ["name", "slug", ("category", "price"), "is_active"]}),
        (_("Details"), {"fields": ["description", "tags", "datasheet"]}),
    ]
    prepopulated_fields = {"slug": ["name"]}
    filter_horizontal = ["tags"]
    widgets = {"category": "rich_select"}
    inlines = [ProductImageInline]
    save_as = True

    @display(description=_("images"), empty_value="—")
    def image_count(self, product):
        return product.images.count() or None


@register(Customer)
class CustomerOptions(ModelOptions):
    list_display = ["name", "phone", "city", "city__province", "created_at"]
    search_fields = ["name", "^phone"]
    list_filter = ["city", "created_at"]
    row_rules = [in_region("city")]


@semantic.register
class OrderMetrics(semantic.Semantic):
    """The semantic model sketched in DESIGN.md §3.2."""

    model = Order
    label = {"fa": "سفارش‌ها", "en": "Orders"}
    synonyms = {"fa": ["فروش", "خرید"], "en": ["sales"]}
    description = {
        "fa": "سفارش‌های فروشگاه با مبلغ، وضعیت، مشتری و شهر",
        "en": "Shop orders with amount, status, customer and city",
    }
    time_dimension = "created_at"
    # Few-shot examples for the NL interface (also the example questions in the UI).
    nl_examples = [
        {
            "question": "Revenue by city in the last 3 months",
            "spec": {
                "semantic_model": "order_metrics",
                "measures": ["revenue"],
                "dimensions": ["city"],
                "relative_period": "last_3_months",
                "chart": "bar",
            },
        },
        {
            "question": "Monthly orders this year",
            "spec": {
                "semantic_model": "order_metrics",
                "measures": ["orders"],
                "time_grain": "month",
                "relative_period": "this_year",
                "chart": "line",
            },
        },
        {
            "question": "Top 5 customers by revenue last year",
            "spec": {
                "semantic_model": "order_metrics",
                "measures": ["revenue"],
                "dimensions": ["customer"],
                "relative_period": "last_year",
                "sort": [{"field": "revenue", "direction": "desc"}],
                "limit": 5,
                "chart": "bar",
            },
        },
        {
            "question": "درآمد به تفکیک شهر در سه ماه گذشته",
            "spec": {
                "semantic_model": "order_metrics",
                "measures": ["revenue"],
                "dimensions": ["city"],
                "relative_period": "last_3_months",
                "chart": "bar",
            },
        },
        {
            "question": "تعداد سفارش‌های ماهانه امسال",
            "spec": {
                "semantic_model": "order_metrics",
                "measures": ["orders"],
                "time_grain": "month",
                "relative_period": "this_year",
                "chart": "line",
            },
        },
        {
            "question": "فروش تهران به تفکیک وضعیت در ماه گذشته",
            "spec": {
                "semantic_model": "order_metrics",
                "measures": ["revenue"],
                "dimensions": ["status"],
                "filters": [{"field": "city", "op": "eq", "value": "تهران"}],
                "relative_period": "last_month",
                "chart": "bar",
            },
        },
    ]

    dimensions = {
        "created_at": semantic.Dimension(
            "created_at",
            type="time",
            label={"fa": "تاریخ ثبت سفارش", "en": "Order date"},
            synonyms={"fa": ["تاریخ سفارش", "زمان ثبت"], "en": ["created", "date"]},
        ),
        "paid_at": semantic.Dimension(
            "paid_at",
            type="time",
            label={"fa": "تاریخ پرداخت", "en": "Payment date"},
            synonyms={"fa": ["زمان پرداخت"], "en": ["paid date"]},
        ),
        "city": semantic.Dimension(
            "customer__city__name",
            type="category",
            label={"fa": "شهر", "en": "City"},
            synonyms={"fa": ["محل", "شهرستان"], "en": ["town"]},
        ),
        "province": semantic.Dimension(
            "customer__city__province",
            type="geo",
            label={"fa": "استان", "en": "Province"},
            synonyms={"fa": ["منطقه"], "en": ["region", "state"]},
        ),
        "status": semantic.Dimension(
            "status",
            type="category",
            label={"fa": "وضعیت", "en": "Status"},
            synonyms={"fa": ["وضعیت سفارش"], "en": ["order status"]},
        ),
        "customer": semantic.Dimension(
            "customer__name",
            label={"fa": "مشتری", "en": "Customer"},
            synonyms={"fa": ["خریدار"], "en": ["buyer", "client"]},
        ),
        "amount": semantic.Dimension(
            "amount",
            type="number",
            label={"fa": "مبلغ سفارش", "en": "Order amount"},
            format="currency",
        ),
    }

    measures = {
        "revenue": semantic.Measure(
            Sum("amount", filter=Q(status=Order.Status.PAID)),
            label={"fa": "درآمد", "en": "Revenue"},
            synonyms={
                "fa": ["فروش", "گردش مالی", "دریافتی"],
                "en": ["sales", "income", "turnover", "spend", "spent"],
            },
            description={
                "fa": "جمع مبلغ سفارش‌های پرداخت‌شده",
                "en": "Sum of amounts of paid orders",
            },
            format="currency",
        ),
        "orders": semantic.Measure(
            Count("id"),
            label={"fa": "تعداد سفارش", "en": "Orders"},
            synonyms={"fa": ["سفارش‌ها", "تعداد"], "en": ["order count"]},
            description={"fa": "تعداد همه سفارش‌ها", "en": "Number of orders, any status"},
            format="integer",
        ),
        "paid_orders": semantic.Measure(
            Count("id", filter=Q(status=Order.Status.PAID)),
            label={"fa": "سفارش‌های پرداخت‌شده", "en": "Paid orders"},
            format="integer",
        ),
        "aov": semantic.Measure(
            Avg("amount", filter=Q(status=Order.Status.PAID)),
            label={"fa": "میانگین سبد", "en": "Avg order value"},
            synonyms={
                "fa": ["میانگین سفارش", "متوسط خرید", "میانگین مبلغ سفارش"],
                "en": ["aov", "average basket", "average order value", "average order"],
            },
            description={
                "fa": "میانگین مبلغ سفارش‌های پرداخت‌شده",
                "en": "Average amount of paid orders",
            },
            format="currency",
        ),
        # Semantic visibility: only users who may change orders (finance) see it.
        "refunds": semantic.Measure(
            Sum("amount", filter=Q(status=Order.Status.REFUNDED)),
            label={"fa": "مبلغ مرجوعی", "en": "Refunds"},
            synonyms={"fa": ["برگشتی", "بازپرداخت"], "en": ["refunded amount"]},
            description={
                "fa": "جمع مبلغ سفارش‌های مرجوع‌شده",
                "en": "Sum of amounts of refunded orders",
            },
            format="currency",
            permission="shop.change_order",
        ),
        "customers": semantic.Measure(
            Count("customer", distinct=True),
            label={"fa": "تعداد مشتری", "en": "Customers"},
            synonyms={"fa": ["خریداران"], "en": ["buyers"]},
            description={"fa": "تعداد مشتریان یکتا", "en": "Distinct customers"},
            format="integer",
        ),
    }
