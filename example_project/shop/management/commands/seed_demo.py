"""Deterministic demo data: cities, customers, products, orders over ~2 years.

``--locale en`` (default) writes English names; ``--locale fa`` the Persian data
set (useful for RTL demos and Arabic/Persian letter search normalisation). Both
locales draw the same random numbers, so a given seed yields the same orders,
amounts and dates either way — only the names differ.
"""

import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from shop.models import City, Customer, Order, OrderItem, Product

LOCALES = ("en", "fa")

# (Persian name, English name, Persian province, English province)
CITIES = [
    ("تهران", "Tehran", "تهران", "Tehran"),
    ("مشهد", "Mashhad", "خراسان رضوی", "Razavi Khorasan"),
    ("اصفهان", "Isfahan", "اصفهان", "Isfahan"),
    ("شیراز", "Shiraz", "فارس", "Fars"),
    ("تبریز", "Tabriz", "آذربایجان شرقی", "East Azerbaijan"),
    ("کرج", "Karaj", "البرز", "Alborz"),
    ("اهواز", "Ahvaz", "خوزستان", "Khuzestan"),
    ("قم", "Qom", "قم", "Qom"),
    ("رشت", "Rasht", "گیلان", "Gilan"),
    ("کرمان", "Kerman", "کرمان", "Kerman"),
]
# Relative order volume per city (Tehran dominates, as in real data).
CITY_WEIGHTS = [30, 12, 10, 9, 8, 8, 6, 6, 5, 6]

# (Persian name, English name, category, price in rials)
PRODUCTS = [
    ("گوشی هوشمند", "Smartphone", "electronics", 25_000_000),
    ("هدفون بی‌سیم", "Wireless headphones", "electronics", 4_500_000),
    ("لپ‌تاپ", "Laptop", "electronics", 60_000_000),
    ("رمان", "Novel", "books", 350_000),
    ("کتاب آشپزی", "Cookbook", "books", 600_000),
    ("تی‌شرت", "T-shirt", "clothing", 450_000),
    ("کاپشن", "Winter jacket", "clothing", 3_200_000),
    ("برنج ایرانی ۱۰ کیلو", "Persian rice 10 kg", "grocery", 2_800_000),
    ("چای ۵۰۰ گرم", "Tea 500 g", "grocery", 400_000),
    ("زعفران ۵ گرم", "Saffron 5 g", "grocery", 1_100_000),
    ("سرویس قابلمه", "Cookware set", "home", 7_500_000),
    ("سماور برقی", "Electric samovar", "home", 3_900_000),
]

# Parallel lists: the same random draws pick the same person in either locale.
FIRST_NAMES = {
    "fa": ["علی", "زهرا", "محمد", "فاطمه", "حسین", "مریم", "رضا", "سارا", "امیر", "نرگس"],
    "en": [
        "Ali",
        "Zahra",
        "Mohammad",
        "Fatemeh",
        "Hossein",
        "Maryam",
        "Reza",
        "Sara",
        "Amir",
        "Narges",
    ],
}
LAST_NAMES = {
    "fa": ["محمدی", "حسینی", "رضایی", "احمدی", "کریمی", "موسوی", "جعفری", "صادقی"],
    "en": ["Mohammadi", "Hosseini", "Rezaei", "Ahmadi", "Karimi", "Mousavi", "Jafari", "Sadeghi"],
}

STATUSES = ["paid", "paid", "paid", "paid", "paid", "paid", "pending", "cancelled", "refunded"]


class Command(BaseCommand):
    help = "Populate the demo shop with deterministic sample data."

    def add_arguments(self, parser):
        parser.add_argument("--seed", type=int, default=42)
        parser.add_argument("--customers", type=int, default=300)
        parser.add_argument("--orders", type=int, default=3000)
        parser.add_argument("--days", type=int, default=730, help="history length")
        parser.add_argument("--flush", action="store_true", help="delete existing shop data")
        parser.add_argument(
            "--locale", choices=LOCALES, default="en", help="language of names (default: en)"
        )

    @transaction.atomic
    def handle(self, *args, seed, customers, orders, days, flush, locale="en", **options):
        if locale not in LOCALES:  # call_command() kwargs skip argparse's choices
            raise CommandError(f"Unknown locale {locale!r}; choose one of {', '.join(LOCALES)}")
        rng = random.Random(seed)
        now = timezone.now()

        if flush:
            OrderItem.objects.all().delete()
            Order.objects.all().delete()
            Customer.objects.all().delete()
            Product.objects.all().delete()
            City.objects.all().delete()

        fa = locale == "fa"
        cities = [
            City.objects.get_or_create(
                name=fa_name if fa else en_name,
                defaults={"name_en": en_name, "province": fa_prov if fa else en_prov},
            )[0]
            for fa_name, en_name, fa_prov, en_prov in CITIES
        ]
        products = [
            Product.objects.get_or_create(
                name=fa_name if fa else en_name,
                defaults={"category": c, "price": Decimal(price)},
            )[0]
            for fa_name, en_name, c, price in PRODUCTS
        ]
        first_names, last_names = FIRST_NAMES[locale], LAST_NAMES[locale]

        new_customers = Customer.objects.bulk_create(
            Customer(
                name=f"{rng.choice(first_names)} {rng.choice(last_names)}",
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
                f"Seeded ({locale}) {len(cities)} cities, {len(products)} products, "
                f"{len(new_customers)} customers, {len(created_orders)} orders."
            )
        )
