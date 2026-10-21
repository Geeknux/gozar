"""Deterministic demo data: cities, customers, products, orders over ~2 years.

``--locale en`` (default) writes English names; ``--locale fa`` the Persian data
set (useful for RTL demos and Arabic/Persian letter search normalisation). Both
locales draw the same random numbers, so a given seed yields the same orders,
amounts and dates either way — only the names differ.

It also creates the access-control demo groups (see ``shop/gozar.py``): the
sales-region groups limited by row rules to their cities, and "Shop managers"
(all shop data, the finance-only Refunds measure, the report audit log and
the question log).
``--users`` adds demo accounts in them, all with the password ``--password``,
and the M6 demo content owned by ``manager`` and shared with everyone: saved
reports, the "Sales overview" dashboard (pinned as manager's home page) and a
weekly e-mail of it to manager, tehran and east — each recipient sees their own
region (see ``DEMO_REPORTS``/``demo_content``). Their names and descriptions are
in English and Persian, so each viewer gets their own language.
``--content-only`` refreshes just that content in an already seeded database.
"""

import datetime as dt
import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from bi_admin import scheduling
from bi_admin.execution import cache
from bi_admin.models import Dashboard, DashboardWidget, SavedReport, Schedule, UserPreferences
from shop.gozar import REGIONS
from shop.models import City, Customer, Order, OrderItem, Product, ProductImage, Tag

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

# (English, Persian) tag names and the products (by index in PRODUCTS) they label
TAGS = [
    (("Bestseller", "پرفروش"), [0, 2, 9]),
    (("Gift idea", "پیشنهاد هدیه"), [1, 9, 11]),
    (("Made in Iran", "تولید ایران"), [7, 8, 9, 11]),
    (("Seasonal", "فصلی"), [6]),
]
DESCRIPTIONS = {
    "electronics": ("Ships with a one-year warranty.", "با یک سال گارانتی ارسال می‌شود."),
    "books": ("Paperback, in stock in every city.", "جلد شومیز، موجود در همه شهرها."),
    "clothing": ("Available in sizes S to XL.", "در اندازه‌های S تا XL موجود است."),
    "grocery": ("Packed fresh every week.", "هر هفته تازه بسته‌بندی می‌شود."),
    "home": ("Free delivery in Tehran.", "ارسال رایگان در تهران."),
}
# Product pictures drawn at seed time (files in MEDIA_ROOT through the storage API)
IMAGE_COLORS = ["#5b4bdb", "#1f8a4c", "#c93636", "#d98e04", "#1b7a9c", "#7a4b9c"]

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

SHOP_MODELS = ["city", "customer", "product", "order", "orderitem"]
MANAGERS = "Shop managers"
# (username, groups, superuser) — created by --users
DEMO_USERS = [
    ("admin", [], True),
    ("manager", [MANAGERS], False),
    ("tehran", ["Sales: Tehran region"], False),
    ("east", ["Sales: East region"], False),
]

ORDERS = "order_metrics"
# name → (description, Persian name and description, spec) — saved reports owned by
# manager, shared with everyone; the English texts are the project's default language
DEMO_REPORTS = {
    "Revenue by city": (
        "Paid revenue per city over the last three complete months.",
        ("درآمد به تفکیک شهر", "درآمد پرداخت‌شدهٔ هر شهر در سه ماه کامل گذشته."),
        {
            "semantic_model": ORDERS,
            "measures": ["revenue"],
            "dimensions": ["city"],
            "relative_period": "last_3_months",
            "chart": "bar",
        },
    ),
    "Monthly revenue vs last year": (
        "Revenue per month for the past 12 months, next to the same months a year earlier.",
        (
            "درآمد ماهانه در برابر پارسال",
            "درآمد هر ماه در ۱۲ ماه گذشته، کنار همان ماه‌ها در یک سال پیش.",
        ),
        {
            "semantic_model": ORDERS,
            "measures": ["revenue"],
            "time_grain": "month",
            "relative_period": "past_12_months",
            "compare_to": "same_period_last_year",
            "chart": "line",
        },
    ),
    "Top 5 customers": (
        "The five customers with the highest revenue last year.",
        ("۵ مشتری برتر", "پنج مشتری با بیشترین درآمد در سال گذشته."),
        {
            "semantic_model": ORDERS,
            "measures": ["revenue", "orders"],
            "dimensions": ["customer"],
            "relative_period": "last_year",
            "sort": [{"field": "revenue", "direction": "desc"}],
            "limit": 5,
        },
    ),
    "This month vs last month": (
        "Month-to-date revenue, orders and average order value against the same days "
        "of last month.",
        (
            "این ماه در برابر ماه قبل",
            "درآمد، تعداد سفارش و میانگین سبد از ابتدای ماه تا امروز، در برابر همان روزهای "
            "ماه قبل.",
        ),
        {
            "semantic_model": ORDERS,
            "measures": ["revenue", "orders", "aov"],
            "relative_period": "this_month",
            "compare_to": "previous_period",
            "chart": "kpi",
        },
    ),
}
DASHBOARD = "Sales overview"
# Persian texts of the dashboard, its inline widget and its weekly e-mail
DASHBOARD_TEXTS = {
    "description": (
        "Revenue, orders and customers at a glance. Use the filters above to narrow every "
        "widget to a period, city or status.",
        "درآمد، سفارش‌ها و مشتریان در یک نگاه. با فیلترهای بالا همهٔ ویجت‌ها را به یک دوره، "
        "شهر یا وضعیت محدود کنید.",
    ),
    "name_fa": "نمای کلی فروش",
}


def _texts(pair: tuple[str, str]) -> dict[str, str]:
    """``(English, Persian)`` as a ``<field>_i18n`` mapping."""
    return {"en": pair[0], "fa": pair[1]}


STATUS_WIDGET = ("Orders by status", "سفارش‌ها بر اساس وضعیت")
SCHEDULE = {
    "name": ("Weekly sales overview", "نمای کلی هفتگی فروش"),
    "description": (
        "Every Saturday at 08:00 Tehran time to the manager and the regional sales teams; "
        "each recipient sees only their own region.",
        "هر شنبه ساعت ۸ صبح به وقت تهران برای مدیر و تیم‌های فروش منطقه‌ای؛ هر گیرنده فقط "
        "منطقهٔ خودش را می‌بیند.",
    ),
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
        parser.add_argument(
            "--users", action="store_true", help="create the demo accounts (see module doc)"
        )
        parser.add_argument("--password", default="gozar-demo", help="password of demo accounts")
        parser.add_argument(
            "--content-only",
            action="store_true",
            help="only refresh the demo content (saved reports, dashboard, e-mail schedule) "
            "of an already seeded database; orders, customers and users are left alone",
        )
        parser.add_argument(
            "--no-media",
            dest="media",
            action="store_false",
            help="skip the product pictures and datasheet (faster; tests use it)",
        )

    @transaction.atomic
    def handle(
        self,
        *args,
        seed,
        customers,
        orders,
        days,
        flush,
        locale="en",
        users=False,
        password="gozar-demo",
        media=True,
        content_only=False,
        **options,
    ):
        if content_only:
            self.demo_content()
            self.stdout.write(self.style.SUCCESS("Refreshed the demo content."))
            return
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
        self.catalogue(products, fa, media)
        cache.bump_all()  # bulk writes send no signals; drop cached report results
        groups = self.access_groups()
        accounts = self.demo_users(groups, password) if users else []
        if users:
            self.demo_content()

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded ({locale}) {len(cities)} cities, {len(products)} products, "
                f"{len(new_customers)} customers, {len(created_orders)} orders, "
                f"{len(groups)} groups" + (f", users: {', '.join(accounts)}." if accounts else ".")
            )
        )

    def catalogue(self, products: list[Product], fa: bool, media: bool = True) -> None:
        """Slugs, descriptions, tags, a datasheet and pictures for the demo's product
        pages, inlines and file fields (idempotent: existing ones are kept)."""
        tags = []
        for (en_name, fa_name), indexes in TAGS:
            tag, _created = Tag.objects.get_or_create(name=fa_name if fa else en_name)
            tags.append((tag, indexes))
        for i, product in enumerate(products):
            if not product.slug:
                product.slug = slugify(product.name, allow_unicode=True)
            if not product.description:
                en_text, fa_text = DESCRIPTIONS[product.category]
                product.description = fa_text if fa else en_text
            if media and i == 2 and not product.datasheet:
                product.datasheet.save(
                    "laptop-datasheet.pdf", ContentFile(_pdf(product.name)), save=False
                )
            product.save()
            product.tags.add(*[tag for tag, indexes in tags if i in indexes])
            if media and i < 8 and not product.images.exists():
                for position in range(1 + (i % 2)):
                    color = IMAGE_COLORS[(i + position) % len(IMAGE_COLORS)]
                    image = ProductImage(product=product, position=position)
                    image.caption = (
                        ["Front", "Side"][position] if not fa else ["روبه‌رو", "کنار"][position]
                    )
                    data = _picture(product.name, color, position)
                    if data is None:
                        break  # Pillow isn't installed: no pictures
                    image.image.save(
                        f"{product.slug or product.pk}-{position}.png",
                        ContentFile(data),
                        save=False,
                    )
                    image.save()

    def access_groups(self) -> dict[str, Group]:
        """The region groups (view shop data, limited by row rules) and managers."""

        def perms(*codenames):
            return Permission.objects.filter(
                content_type__app_label__in=["shop", "bi_admin"], codename__in=codenames
            )

        view = [f"view_{m}" for m in SHOP_MODELS]
        groups = {}
        for name in [*REGIONS, MANAGERS]:
            group, _created = Group.objects.get_or_create(name=name)
            extra = ["change_order", "view_reportlog", "view_nllog"] if name == MANAGERS else []
            group.permissions.set(perms(*view, *extra))
            groups[name] = group
        return groups

    def demo_users(self, groups: dict[str, Group], password: str) -> list[str]:
        User = get_user_model()
        names = []
        for username, group_names, superuser in DEMO_USERS:
            user, _created = User.objects.get_or_create(
                username=username, defaults={"email": f"{username}@example.com"}
            )
            user.is_staff = True
            user.is_superuser = superuser
            user.set_password(password)
            user.save()
            user.groups.set([groups[g] for g in group_names])
            names.append(username)
        return names

    def demo_content(self) -> None:
        """Saved reports, the "Sales overview" dashboard and its weekly e-mail
        (idempotent: re-running updates them in place)."""
        User = get_user_model()
        manager = User.objects.get(username="manager")
        everyone = SavedReport.Visibility.EVERYONE
        saved = {}
        for name, (description, (fa_name, fa_description), spec) in DEMO_REPORTS.items():
            saved[name], _created = SavedReport.objects.update_or_create(
                owner=manager,
                name=name,
                defaults={
                    "description": description,
                    "name_i18n": {"en": name, "fa": fa_name},
                    "description_i18n": {"en": description, "fa": fa_description},
                    "semantic_model": spec["semantic_model"],
                    "spec": spec,
                    "visibility": everyone,
                },
            )
        board, _created = Dashboard.objects.update_or_create(
            owner=manager,
            name=DASHBOARD,
            defaults={
                "description": DASHBOARD_TEXTS["description"][0],
                "name_i18n": {"en": DASHBOARD, "fa": DASHBOARD_TEXTS["name_fa"]},
                "description_i18n": _texts(DASHBOARD_TEXTS["description"]),
                "visibility": everyone,
                "filters": {"dimensions": ["city", "status"]},
            },
        )
        board.widgets.all().delete()
        status_spec = {
            "semantic_model": ORDERS,
            "measures": ["orders"],
            "dimensions": ["status"],
            "relative_period": "last_3_months",
            "chart": "pie",
        }
        DashboardWidget.objects.bulk_create(
            [
                DashboardWidget(
                    dashboard=board, saved_report=saved["This month vs last month"],
                    chart="kpi", x=0, y=0, w=12, h=2,
                ),
                DashboardWidget(
                    dashboard=board, saved_report=saved["Monthly revenue vs last year"],
                    chart="line", x=0, y=2, w=8, h=4,
                ),
                DashboardWidget(
                    dashboard=board, title=STATUS_WIDGET[0],
                    title_i18n=_texts(STATUS_WIDGET), spec=status_spec,
                    chart="pie", x=8, y=2, w=4, h=4,
                ),
                DashboardWidget(
                    dashboard=board, saved_report=saved["Revenue by city"],
                    chart="bar", x=0, y=6, w=6, h=4,
                ),
                DashboardWidget(
                    dashboard=board, saved_report=saved["Top 5 customers"],
                    chart="table", x=6, y=6, w=6, h=4,
                ),
            ]
        )  # fmt: skip
        prefs, _created = UserPreferences.objects.get_or_create(user=manager)
        prefs.options = {**prefs.options, "home_dashboard": board.pk}
        prefs.save()
        schedule, _created = Schedule.objects.update_or_create(
            owner=manager,
            dashboard=board,
            defaults={
                "name": SCHEDULE["name"][0],
                "name_i18n": _texts(SCHEDULE["name"]),
                "description": SCHEDULE["description"][0],
                "description_i18n": _texts(SCHEDULE["description"]),
                "frequency": Schedule.Frequency.WEEKLY,
                "weekday": 5,  # Saturday, the first day of the Iranian week
                "time": dt.time(8, 0),
                "calendar": "gregorian",
                "timezone": "Asia/Tehran",
                "attachment": Schedule.Attachment.XLSX,
                "enabled": True,
            },
        )
        schedule.recipients.set(User.objects.filter(username__in=["manager", "tehran", "east"]))
        schedule.next_run_at = scheduling.next_run(schedule)
        schedule.save(update_fields=["next_run_at"])


def _picture(name: str, color: str, variant: int) -> bytes | None:
    """A small PNG with the product's initials (needs Pillow, the demo's dependency)."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    import io

    img = Image.new("RGB", (320, 240), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([16, 16, 303, 223], outline="white", width=3 if variant == 0 else 1)
    initials = "".join(w[0] for w in name.split()[:2] if w[:1].isascii()).upper() or "#"
    draw.text((160, 120), initials, fill="white", anchor="mm", font_size=72)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _pdf(title: str) -> bytes:
    """A one-page PDF datasheet."""
    text = f"{title} datasheet".encode("ascii", "replace").decode()
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return out
