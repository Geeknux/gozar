"""Deterministic demo data: cities, customers, products, orders over ~2 years."""

import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from shop.models import City, Customer, Order, OrderItem, Product

CITIES = [
    ("تهران", "Tehran", "تهران"),
    ("مشهد", "Mashhad", "خراسان رضوی"),
    ("اصفهان", "Isfahan", "اصفهان"),
    ("شیراز", "Shiraz", "فارس"),
    ("تبریز", "Tabriz", "آذربایجان شرقی"),
    ("کرج", "Karaj", "البرز"),
    ("اهواز", "Ahvaz", "خوزستان"),
    ("قم", "Qom", "قم"),
    ("رشت", "Rasht", "گیلان"),
    ("کرمان", "Kerman", "کرمان"),
]
# Relative order volume per city (Tehran dominates, as in real data).
CITY_WEIGHTS = [30, 12, 10, 9, 8, 8, 6, 6, 5, 6]

PRODUCTS = [
    ("گوشی هوشمند", "electronics", 25_000_000),
    ("هدفون بی‌سیم", "electronics", 4_500_000),
    ("لپ‌تاپ", "electronics", 60_000_000),
    ("رمان", "books", 350_000),
    ("کتاب آشپزی", "books", 600_000),
    ("تی‌شرت", "clothing", 450_000),
    ("کاپشن", "clothing", 3_200_000),
    ("برنج ایرانی ۱۰ کیلو", "grocery", 2_800_000),
    ("چای ۵۰۰ گرم", "grocery", 400_000),
    ("زعفران ۵ گرم", "grocery", 1_100_000),
    ("سرویس قابلمه", "home", 7_500_000),
    ("سماور برقی", "home", 3_900_000),
]

FIRST_NAMES = ["علی", "زهرا", "محمد", "فاطمه", "حسین", "مریم", "رضا", "سارا", "امیر", "نرگس"]
LAST_NAMES = ["محمدی", "حسینی", "رضایی", "احمدی", "کریمی", "موسوی", "جعفری", "صادقی"]

STATUSES = ["paid", "paid", "paid", "paid", "paid", "paid", "pending", "cancelled", "refunded"]


class Command(BaseCommand):
    help = "Populate the demo shop with deterministic sample data."

    def add_arguments(self, parser):
        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument("--customers", type=int, default=300)
        parser.add_argument("--orders", type=int, default=3000)
        parser.add_argument("--days", type=int, default=730, help="history length")
        parser.add_argument("--flush", action="store_true", help="delete existing shop data")

    @transaction.atomic
    def handle(self, *args, seed, customers, orders, days, flush, **options):
        rng = random.Random(seed)
        now = timezone.now()

        if flush:
            OrderItem.objects.all().delete()
            Order.objects.all().delete()
            Customer.objects.all().delete()
            Product.objects.all().delete()
            City.objects.all().delete()

        cities = [
            City.objects.get_or_create(name=n, defaults={"name_en": en, "province": p})[0]
            for n, en, p in CITIES
        ]
        products = [
            Product.objects.get_or_create(
                name=n, defaults={"category": c, "price": Decimal(price)}
            )[0]
            for n, c, price in PRODUCTS
        ]

        new_customers = Customer.objects.bulk_create(
            Customer(
                name=f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
                phone=f"09{rng.randint(100000000, 999999999)}",
                city=rng.choices(cities, weights=CITY_WEIGHTS)[0],
                created_at=now - timedelta(days=rng.randint(days, days + 365)),
            )
            for _ in range(customers)
        )

        order_rows = []
        item_rows = []
        for _ in range(orders):
            # Skew towards recent dates to give a growth trend.
            age = int(days * (1 - rng.random() ** 0.7))
            created = now - timedelta(days=age, minutes=rng.randint(0, 1439))
            status = rng.choice(STATUSES)
            lines = [(p, rng.randint(1, 3)) for p in rng.sample(products, k=rng.randint(1, 4))]
            amount = sum(p.price * q for p, q in lines)
            order_rows.append(
                Order(
                    customer=rng.choice(new_customers),
                    status=status,
                    amount=amount,
                    created_at=created,
                    paid_at=(
                        created + timedelta(minutes=rng.randint(1, 600))
                        if status in ("paid", "refunded")
                        else None
                    ),
                )
            )
            item_rows.append(lines)

        created_orders = Order.objects.bulk_create(order_rows, batch_size=1000)
        OrderItem.objects.bulk_create(
            (
                OrderItem(order=o, product=p, quantity=q, unit_price=p.price)
                for o, lines in zip(created_orders, item_rows, strict=True)
                for p, q in lines
            ),
            batch_size=2000,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(cities)} cities, {len(products)} products, "
                f"{len(new_customers)} customers, {len(created_orders)} orders."
            )
        )
