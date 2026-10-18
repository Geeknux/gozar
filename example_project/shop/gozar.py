"""Gozar options for the demo shop (auto-discovered, like admin.py)."""

from django.db.models import Avg, Count, Q, Sum

from bi_admin import semantic
from bi_admin.crud import ModelOptions, action, register
from shop.models import Customer, Order


@register(Order)
class OrderOptions(ModelOptions):
    list_display = ["__str__", "customer", "customer__city__name", "status", "amount", "created_at"]
    search_fields = ["customer__name", "customer__phone"]
    list_filter = ["status", "created_at", "paid_at", "customer__city"]
    ordering = ["-created_at"]
    readonly_fields = ["paid_at"]
    actions = ["mark_cancelled"]

    @action("لغو سفارش‌های انتخاب‌شده", permission="change")
    def mark_cancelled(self, request, queryset):
        updated = queryset.filter(status=Order.Status.PENDING).update(status=Order.Status.CANCELLED)
        return {"updated": updated}


@register(Customer)
class CustomerOptions(ModelOptions):
    list_display = ["name", "phone", "city", "city__province", "created_at"]
    search_fields = ["name", "^phone"]
    list_filter = ["city", "created_at"]


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
            synonyms={"fa": ["فروش", "گردش مالی", "دریافتی"], "en": ["sales"]},
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
            synonyms={"fa": ["میانگین سفارش", "متوسط خرید"], "en": ["aov", "average basket"]},
            description={
                "fa": "میانگین مبلغ سفارش‌های پرداخت‌شده",
                "en": "Average amount of paid orders",
            },
            format="currency",
        ),
        "customers": semantic.Measure(
            Count("customer", distinct=True),
            label={"fa": "تعداد مشتری", "en": "Customers"},
            synonyms={"fa": ["خریداران"], "en": ["buyers"]},
            description={"fa": "تعداد مشتریان یکتا", "en": "Distinct customers"},
            format="integer",
        ),
    }
