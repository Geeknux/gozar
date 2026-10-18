"""Demo domain used by tests and the semantic-layer examples in DESIGN.md."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class City(models.Model):
    name = models.CharField(_("name"), max_length=100, unique=True)
    name_en = models.CharField(_("English name"), max_length=100, blank=True)
    province = models.CharField(_("province"), max_length=100)

    class Meta:
        verbose_name = _("city")
        verbose_name_plural = _("cities")
        ordering = ["name"]

    def __str__(self):
        return self.name


class Customer(models.Model):
    name = models.CharField(_("name"), max_length=200)
    phone = models.CharField(_("phone"), max_length=20, blank=True)
    city = models.ForeignKey(
        City, on_delete=models.PROTECT, related_name="customers", verbose_name=_("city")
    )
    created_at = models.DateTimeField(_("joined at"))

    class Meta:
        verbose_name = _("customer")
        verbose_name_plural = _("customers")

    def __str__(self):
        return self.name


class Product(models.Model):
    class Category(models.TextChoices):
        ELECTRONICS = "electronics", _("Electronics")
        BOOKS = "books", _("Books")
        CLOTHING = "clothing", _("Clothing")
        GROCERY = "grocery", _("Grocery")
        HOME = "home", _("Home & kitchen")

    name = models.CharField(_("name"), max_length=200)
    category = models.CharField(_("category"), max_length=20, choices=Category.choices)
    price = models.DecimalField(_("price"), max_digits=14, decimal_places=0)

    class Meta:
        verbose_name = _("product")
        verbose_name_plural = _("products")

    def __str__(self):
        return self.name


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", _("Awaiting payment")
        PAID = "paid", _("Paid")
        CANCELLED = "cancelled", _("Cancelled")
        REFUNDED = "refunded", _("Refunded")

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="orders", verbose_name=_("customer")
    )
    status = models.CharField(
        _("status"), max_length=20, choices=Status.choices, default=Status.PENDING
    )
    amount = models.DecimalField(_("amount"), max_digits=16, decimal_places=0, default=0)
    created_at = models.DateTimeField(_("created at"), db_index=True)
    paid_at = models.DateTimeField(_("paid at"), null=True, blank=True)

    class Meta:
        verbose_name = _("order")
        verbose_name_plural = _("orders")

    def __str__(self):
        return f"#{self.pk}"


class OrderItem(models.Model):
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="items", verbose_name=_("order")
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="order_items", verbose_name=_("product")
    )
    quantity = models.PositiveIntegerField(_("quantity"), default=1)
    unit_price = models.DecimalField(_("unit price"), max_digits=14, decimal_places=0)

    class Meta:
        verbose_name = _("order item")
        verbose_name_plural = _("order items")

    def __str__(self):
        return f"{self.product} × {self.quantity}"
