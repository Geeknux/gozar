"""Demo domain used by tests and the semantic-layer examples in DESIGN.md."""

from django.db import models


class City(models.Model):
    name = models.CharField("نام", max_length=100, unique=True)
    name_en = models.CharField("نام انگلیسی", max_length=100, blank=True)
    province = models.CharField("استان", max_length=100)

    class Meta:
        verbose_name = "شهر"
        verbose_name_plural = "شهرها"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Customer(models.Model):
    name = models.CharField("نام", max_length=200)
    phone = models.CharField("تلفن", max_length=20, blank=True)
    city = models.ForeignKey(
        City, on_delete=models.PROTECT, related_name="customers", verbose_name="شهر"
    )
    created_at = models.DateTimeField("تاریخ عضویت")

    class Meta:
        verbose_name = "مشتری"
        verbose_name_plural = "مشتریان"

    def __str__(self):
        return self.name


class Product(models.Model):
    class Category(models.TextChoices):
        ELECTRONICS = "electronics", "الکترونیک"
        BOOKS = "books", "کتاب"
        CLOTHING = "clothing", "پوشاک"
        GROCERY = "grocery", "خواربار"
        HOME = "home", "خانه و آشپزخانه"

    name = models.CharField("نام", max_length=200)
    category = models.CharField("دسته", max_length=20, choices=Category.choices)
    price = models.DecimalField("قیمت", max_digits=14, decimal_places=0)

    class Meta:
        verbose_name = "محصول"
        verbose_name_plural = "محصولات"

    def __str__(self):
        return self.name


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "در انتظار پرداخت"
        PAID = "paid", "پرداخت‌شده"
        CANCELLED = "cancelled", "لغوشده"
        REFUNDED = "refunded", "مرجوعی"

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="orders", verbose_name="مشتری"
    )
    status = models.CharField(
        "وضعیت", max_length=20, choices=Status.choices, default=Status.PENDING
    )
    amount = models.DecimalField("مبلغ", max_digits=16, decimal_places=0, default=0)
    created_at = models.DateTimeField("تاریخ ثبت", db_index=True)
    paid_at = models.DateTimeField("تاریخ پرداخت", null=True, blank=True)

    class Meta:
        verbose_name = "سفارش"
        verbose_name_plural = "سفارش‌ها"

    def __str__(self):
        return f"#{self.pk}"


class OrderItem(models.Model):
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="items", verbose_name="سفارش"
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="order_items", verbose_name="محصول"
    )
    quantity = models.PositiveIntegerField("تعداد", default=1)
    unit_price = models.DecimalField("قیمت واحد", max_digits=14, decimal_places=0)

    class Meta:
        verbose_name = "قلم سفارش"
        verbose_name_plural = "اقلام سفارش"

    def __str__(self):
        return f"{self.product} × {self.quantity}"
