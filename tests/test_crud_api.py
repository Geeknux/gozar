"""M1: CRUD JSON API — list/search/filter/sort, detail, create/update, delete, perms."""

from decimal import Decimal

import pytest
from django.utils import timezone

from bi_admin.models import ChangeLog
from shop.models import City, Customer, Order, OrderItem, Product

API = "/gozar/api"

pytestmark = pytest.mark.django_db


def crud(label, suffix=""):
    app, model = label.split(".")
    return f"{API}/crud/{app}/{model}/{suffix}"


# --- auth & permissions ------------------------------------------------------------


def test_api_requires_staff_session(client, django_user_model):
    assert client.get(f"{API}/meta/models").status_code == 401
    user = django_user_model.objects.create_user("joe", password="pw-123456!")  # not staff
    client.force_login(user)
    assert client.get(f"{API}/meta/models").status_code == 401


def test_login_logout_flow(client, admin_user):
    client.get(f"{API}/auth/csrf")
    assert client.get(f"{API}/auth/me").json()["authenticated"] is False
    bad = client.post(
        f"{API}/auth/login", {"username": "root", "password": "wrong"}, "application/json"
    )
    assert bad.status_code == 401
    ok = client.post(
        f"{API}/auth/login", {"username": "root", "password": "s3cret-pass!"}, "application/json"
    )
    assert ok.status_code == 200 and ok.json()["user"]["username"] == "root"
    assert client.get(f"{API}/auth/me").json()["authenticated"] is True
    client.post(f"{API}/auth/logout")
    assert client.get(f"{API}/auth/me").json()["authenticated"] is False


def test_csrf_enforced_for_session_writes(admin_user):
    from django.test import Client

    c = Client(enforce_csrf_checks=True)
    c.force_login(admin_user)
    resp = c.post(crud("shop.city"), {"name": "یزد", "province": "یزد"}, "application/json")
    assert resp.status_code in (401, 403)
    assert not City.objects.filter(name="یزد").exists()


def test_models_index_respects_permissions(client, make_staff, seeded):
    client.force_login(make_staff("shop.view_order", "shop.change_customer"))
    apps = client.get(f"{API}/meta/models").json()["apps"]
    labels = {m["label"]: m["permissions"] for a in apps for m in a["models"]}
    assert set(labels) == {"shop.order", "shop.customer"}
    assert labels["shop.order"] == {"view": True, "add": False, "change": False, "delete": False}
    assert labels["shop.customer"]["view"] is True  # change implies view

    assert client.get(crud("shop.order")).status_code == 200
    assert client.get(crud("shop.city")).status_code == 403
    first = Order.objects.first()
    assert (
        client.patch(crud("shop.order", f"{first.pk}"), {}, "application/json").status_code == 403
    )
    assert client.delete(crud("shop.order", f"{first.pk}")).status_code == 403


def test_unknown_model_is_404(admin_client):
    assert admin_client.get(crud("shop.nope")).status_code == 404
    assert admin_client.get(crud("contenttypes.contenttype")).status_code == 404


# --- metadata ----------------------------------------------------------------------


def test_model_meta_describes_list_and_form(admin_client, seeded):
    meta = admin_client.get(f"{API}/meta/models/shop/order").json()
    assert meta["verbose_name"] == "order"
    cols = {c["name"]: c for c in meta["list"]["columns"]}
    assert cols["customer__city__name"]["label"] == "Customer / City / Name"
    assert cols["status"]["sortable"] and not cols["__str__"]["sortable"]
    filters = {f["name"]: f for f in meta["list"]["filters"]}
    assert filters["status"]["choices"][0] == {"value": "pending", "label": "Awaiting payment"}
    assert filters["customer__city"]["kind"] == "relation"
    assert len(filters["customer__city"]["choices"]) == 10
    assert filters["created_at"]["kind"] == "datetime"
    assert "paid_at" in meta["form"]["readonly_fields"]
    actions = {a["name"] for a in meta["list"]["actions"]}
    assert actions == {"delete_selected", "mark_cancelled"}


def test_default_options_for_undeclared_model(admin_client, seeded):
    meta = admin_client.get(f"{API}/meta/models/shop/product").json()
    assert [c["name"] for c in meta["list"]["columns"]] == ["__str__", "name", "category", "price"]
    assert meta["list"]["search_fields"] == ["name"]
    assert [f["name"] for f in meta["list"]["filters"]] == ["category"]


def test_user_password_is_write_only(admin_client, admin_user):
    meta = admin_client.get(f"{API}/meta/models/auth/user").json()
    pw = next(f for f in meta["fields"] if f["name"] == "password")
    assert pw["kind"] == "password"
    detail = admin_client.get(crud("auth.user", f"{admin_user.pk}")).json()
    assert detail["values"]["password"] is None
    assert (
        admin_client.get(crud("auth.user"), {"password__startswith": "pbkdf2"}).status_code == 400
    )


def test_graph_and_path(admin_client):
    graph = admin_client.get(f"{API}/meta/graph").json()
    assert any(
        e["source"] == "shop.customer" and e["target"] == "shop.city" for e in graph["edges"]
    )
    path = admin_client.get(f"{API}/meta/path", {"source": "shop.orderitem", "target": "shop.city"})
    assert path.json()["lookup"] == "order__customer__city"


# --- list --------------------------------------------------------------------------


def test_list_pagination_and_cells(admin_client, seeded):
    data = admin_client.get(crud("shop.order"), {"page_size": 25, "page": 2}).json()
    assert data["count"] == 60 and data["num_pages"] == 3 and data["page"] == 2
    assert len(data["rows"]) == 25
    names = [c["name"] for c in data["columns"]]
    row = data["rows"][0]
    cell = dict(zip(names, row["cells"], strict=True))
    order = Order.objects.select_related("customer__city").get(pk=row["pk"])
    assert cell["customer"] == {
        "pk": order.customer_id,
        "label": order.customer.name,
        "model": "shop.customer",
    }
    assert cell["customer__city__name"] == order.customer.city.name
    assert cell["status"] == order.get_status_display()
    assert cell["amount"] == str(order.amount)
    assert admin_client.get(crud("shop.order"), {"page": 99}).status_code == 404


def test_list_default_ordering_and_sort_param(admin_client, seeded):
    data = admin_client.get(crud("shop.order"), {"page_size": 500}).json()
    pks = [r["pk"] for r in data["rows"]]
    assert pks == list(Order.objects.order_by("-created_at", "-id").values_list("pk", flat=True))
    data = admin_client.get(crud("shop.order"), {"ordering": "amount", "page_size": 500}).json()
    amounts = [Decimal(Order.objects.get(pk=r["pk"]).amount) for r in data["rows"]]
    assert amounts == sorted(amounts)
    by_city = admin_client.get(crud("shop.order"), {"ordering": "-customer__city__name"})
    assert by_city.status_code == 200
    assert admin_client.get(crud("shop.order"), {"ordering": "customer__phone"}).status_code == 400


def test_list_filters(admin_client, seeded):
    paid = admin_client.get(crud("shop.order"), {"status": "paid", "page_size": 500}).json()
    assert paid["count"] == Order.objects.filter(status="paid").count()
    assert paid["total"] == 60

    tehran = City.objects.get(name="تهران")
    by_city = admin_client.get(crud("shop.order"), {"customer__city": tehran.pk}).json()
    assert by_city["count"] == Order.objects.filter(customer__city=tehran).count()

    unpaid = admin_client.get(crud("shop.order"), {"paid_at__isnull": "true"}).json()
    assert unpaid["count"] == Order.objects.filter(paid_at__isnull=True).count()

    day = timezone.localdate() - timezone.timedelta(days=30)
    recent = admin_client.get(crud("shop.order"), {"created_at__date__gte": day.isoformat()}).json()
    assert recent["count"] == Order.objects.filter(created_at__date__gte=day).count()

    multi = admin_client.get(crud("shop.order"), {"status__in": "paid,refunded"}).json()
    assert multi["count"] == Order.objects.filter(status__in=["paid", "refunded"]).count()


def test_list_rejects_unknown_filters_and_bad_values(admin_client, seeded):
    assert admin_client.get(crud("shop.order"), {"customer__phone": "09"}).status_code == 400
    assert admin_client.get(crud("shop.order"), {"paid_at__isnull": "maybe"}).status_code == 400
    assert admin_client.get(crud("shop.order"), {"created_at__date__gte": "x"}).status_code == 400
    assert admin_client.get(crud("shop.order"), {"page_size": "x"}).status_code == 400


def test_search_across_relations_and_arabic_letters(admin_client, seeded):
    customer = Customer.objects.first()
    last_name = customer.name.split()[-1]
    data = admin_client.get(crud("shop.order"), {"q": last_name, "page_size": 500}).json()
    assert data["count"] == Order.objects.filter(customer__name__icontains=last_name).count() > 0

    City.objects.create(name="کرمانشاه", province="کرمانشاه")
    arabic = "كرمانشاه"  # Arabic kaf
    found = admin_client.get(crud("shop.city"), {"q": arabic}).json()
    assert [r["label"] for r in found["rows"]] == ["کرمانشاه"]


# --- detail / create / update --------------------------------------------------------


def test_detail_serialization(admin_client, seeded):
    order = Order.objects.exclude(paid_at=None).first()
    data = admin_client.get(crud("shop.order", f"{order.pk}")).json()
    assert data["label"] == f"#{order.pk}"
    v = data["values"]
    assert v["status"] == order.status
    assert v["customer"]["pk"] == order.customer_id
    assert v["amount"] == str(order.amount)
    assert v["paid_at"].startswith(str(order.paid_at.year))
    assert admin_client.get(crud("shop.order", "999999")).status_code == 404
    assert admin_client.get(crud("shop.order", "abc")).status_code == 404


def test_create_validates_and_logs(admin_client, admin_user):
    city = City.objects.create(name="یزد", province="یزد")
    bad = admin_client.post(crud("shop.customer"), {"name": ""}, "application/json")
    assert bad.status_code == 400
    errors = bad.json()["errors"]
    assert {"name", "city", "created_at"} <= set(errors)

    ok = admin_client.post(
        crud("shop.customer"),
        {"name": "مینا", "city": {"pk": city.pk}, "created_at": "2026-01-02T10:00:00+03:30"},
        "application/json",
    )
    assert ok.status_code == 201, ok.json()
    customer = Customer.objects.get(pk=ok.json()["pk"])
    assert customer.city == city and customer.created_at.year == 2026
    log = ChangeLog.objects.get(model_label="shop.customer", object_pk=str(customer.pk))
    assert log.action == "add" and log.user == admin_user


def test_create_rejects_unknown_and_readonly_fields(admin_client, seeded):
    order = Order.objects.first()
    resp = admin_client.patch(
        crud("shop.order", f"{order.pk}"), {"paid_at": "2026-01-01T00:00:00Z"}, "application/json"
    )
    assert resp.status_code == 400 and "paid_at" in resp.json()["errors"]
    resp = admin_client.patch(crud("shop.order", f"{order.pk}"), {"bogus": 1}, "application/json")
    assert resp.status_code == 400


def test_partial_update_keeps_other_fields_and_records_history(admin_client, seeded):
    order = Order.objects.filter(status="pending").first()
    before = (order.customer_id, order.amount, order.created_at)
    resp = admin_client.patch(
        crud("shop.order", f"{order.pk}"), {"status": "cancelled"}, "application/json"
    )
    assert resp.status_code == 200, resp.json()
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert (order.customer_id, order.amount, order.created_at) == before
    hist = admin_client.get(crud("shop.order", f"{order.pk}/history")).json()["entries"]
    assert hist[0]["action"] == "change" and hist[0]["changes"] == {"fields": ["status"]}

    invalid = admin_client.patch(
        crud("shop.order", f"{order.pk}"), {"status": "lost"}, "application/json"
    )
    assert invalid.status_code == 400 and "status" in invalid.json()["errors"]


def test_m2m_and_password_on_user(admin_client):
    from django.contrib.auth.models import Group, User

    g = Group.objects.create(name="فروش")
    resp = admin_client.post(
        crud("auth.user"),
        {"username": "sara", "password": "Very-Strong-Pass-42", "groups": [g.pk], "is_staff": True},
        "application/json",
    )
    assert resp.status_code == 201, resp.json()
    sara = User.objects.get(username="sara")
    assert sara.check_password("Very-Strong-Pass-42")
    assert list(sara.groups.all()) == [g]
    assert resp.json()["values"]["groups"] == [{"pk": g.pk, "label": "فروش", "model": "auth.group"}]

    weak = admin_client.patch(
        crud("auth.user", f"{sara.pk}"), {"password": "123"}, "application/json"
    )
    assert weak.status_code == 400 and "password" in weak.json()["errors"]
    sara.refresh_from_db()
    assert sara.check_password("Very-Strong-Pass-42")

    no_pw = admin_client.post(crud("auth.user"), {"username": "reza"}, "application/json")
    assert no_pw.status_code == 201
    assert not User.objects.get(username="reza").has_usable_password()


def test_new_object_defaults(admin_client):
    data = admin_client.get(crud("shop.order", "new")).json()
    assert data["pk"] is None and data["values"]["status"] == "pending"


# --- autocomplete ------------------------------------------------------------------


def test_autocomplete_uses_target_search(admin_client, seeded):
    resp = admin_client.get(crud("shop.customer", "autocomplete"), {"field": "city", "q": "تهر"})
    assert [r["label"] for r in resp.json()["results"]] == ["تهران"]
    assert (
        admin_client.get(crud("shop.customer", "autocomplete"), {"field": "name"}).status_code
        == 400
    )


def test_autocomplete_for_filter_paths_and_unregistered_targets(admin_client, seeded):
    resp = admin_client.get(
        crud("shop.order", "autocomplete"), {"field": "customer__city", "q": "شیر"}
    )
    assert [r["label"] for r in resp.json()["results"]] == ["شیراز"]
    perms = admin_client.get(
        crud("auth.user", "autocomplete"), {"field": "user_permissions", "q": "order"}
    )
    assert perms.status_code == 200 and perms.json()["results"]


# --- delete ------------------------------------------------------------------------


def test_delete_plan_and_cascade(admin_client, seeded):
    order = Order.objects.annotate().first()
    n_items = order.items.count()
    plan = admin_client.get(crud("shop.order", f"{order.pk}/delete-plan")).json()
    assert plan["deletable"]
    summary = {s["model"]: s["count"] for s in plan["summary"]}
    assert summary == {"shop.order": 1, "shop.orderitem": n_items}

    resp = admin_client.delete(crud("shop.order", f"{order.pk}"))
    assert resp.status_code == 200 and resp.json()["deleted"] == 1
    assert not Order.objects.filter(pk=order.pk).exists()
    assert not OrderItem.objects.filter(order_id=order.pk).exists()
    assert ChangeLog.objects.filter(action="delete", object_pk=str(order.pk)).exists()


def test_delete_blocked_by_protect(admin_client, seeded):
    city = City.objects.filter(customers__isnull=False).first()
    plan = admin_client.get(crud("shop.city", f"{city.pk}/delete-plan")).json()
    assert not plan["deletable"]
    assert plan["protected"][0]["model"] == "shop.customer"
    resp = admin_client.delete(crud("shop.city", f"{city.pk}"))
    assert resp.status_code == 409
    assert City.objects.filter(pk=city.pk).exists()


def test_delete_requires_perms_on_cascaded_models(client, make_staff, seeded):
    client.force_login(make_staff("shop.view_order", "shop.delete_order"))
    order = Order.objects.first()
    plan = client.get(crud("shop.order", f"{order.pk}/delete-plan")).json()
    assert not plan["deletable"] and plan["perms_needed"] == ["order items"]
    assert client.delete(crud("shop.order", f"{order.pk}")).status_code == 409


# --- actions -----------------------------------------------------------------------


def test_bulk_delete_action(admin_client, seeded):
    product = Product.objects.create(name="آزمایشی", category="books", price=1)
    other = Product.objects.create(name="آزمایشی ۲", category="books", price=2)
    pks = [product.pk, other.pk]
    plan = admin_client.post(crud("shop.product", "delete-plan"), {"pks": pks}, "application/json")
    assert plan.json()["deletable"]
    resp = admin_client.post(
        crud("shop.product", "actions/delete_selected"), {"pks": pks}, "application/json"
    )
    assert resp.status_code == 200 and resp.json()["deleted"] == 2


def test_custom_action(admin_client, seeded):
    pending = list(Order.objects.filter(status="pending").values_list("pk", flat=True))
    paid = Order.objects.filter(status="paid").first().pk
    resp = admin_client.post(
        crud("shop.order", "actions/mark_cancelled"), {"pks": [*pending, paid]}, "application/json"
    )
    assert resp.json() == {"action": "mark_cancelled", "updated": len(pending)}
    assert not Order.objects.filter(status="pending").exists()
    assert Order.objects.get(pk=paid).status == "paid"
    assert (
        admin_client.post(
            crud("shop.order", "actions/nope"), {"pks": [paid]}, "application/json"
        ).status_code
        == 404
    )
    assert (
        admin_client.post(
            crud("shop.order", "actions/mark_cancelled"), {"pks": []}, "application/json"
        ).status_code
        == 400
    )


# --- SPA shell ---------------------------------------------------------------------


def test_spa_shell_served_for_client_routes(admin_client):
    for url in ["/gozar/", "/gozar/shop/order/12"]:
        resp = admin_client.get(url)
        assert resp.status_code == 200
        assert b'id="gozar-config"' in resp.content
        assert "csrftoken" in resp.cookies
