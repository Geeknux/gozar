"""Gozar options for the demo shop (auto-discovered, like admin.py)."""

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
