"""M6.5: the user admin — add form with two passwords, the change-password page,
password validators, permission/group pickers and who may grant what."""

import pytest
from django.contrib.auth.models import Group, Permission, User
from django.test import Client

from bi_admin.models import ChangeLog

API = "/gozar/api"
pytestmark = pytest.mark.django_db
STRONG = "Correct-Horse-Battery-9"


def crud(label, suffix=""):
    app, model = label.split(".")
    return f"{API}/crud/{app}/{model}/{suffix}"


def login(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def perms(*codenames):
    return list(Permission.objects.filter(codename__in=codenames).values_list("pk", flat=True))


def test_user_meta_has_admin_layout(admin_client):
    meta = admin_client.get(f"{API}/meta/models/auth/user").json()
    form = meta["form"]
    assert [fs["title"] for fs in form["fieldsets"]] == [
        None,
        "Personal info",
        "Permissions",
        "Important dates",
    ]
    assert form["fieldsets"][1]["rows"][0] == ["first_name", "last_name"]
    assert form["add"]["custom_form"] is True
    assert form["add"]["fields"] == ["username", "usable_password", "password1", "password2"]
    assert form["add"]["fieldsets"][0]["description"].startswith("First, enter a username")
    assert form["change_password"] is True
    assert form["widgets"]["groups"] == {"type": "dual_list", "orientation": "horizontal"}
    assert form["widgets"]["user_permissions"] == {"type": "permissions"}
    assert form["widgets"]["usable_password"]["type"] == "radio"
    assert form["password_help"]  # the validators' help texts
    fields = {f["name"]: f for f in meta["fields"]}
    assert fields["password1"]["kind"] == "password" and fields["password1"]["form_only"]
    assert fields["usable_password"]["choices"]


def test_add_user_with_two_passwords(admin_client):
    resp = admin_client.post(
        crud("auth.user"),
        {"username": "nima", "usable_password": "true", "password1": STRONG, "password2": STRONG},
        "application/json",
    )
    assert resp.status_code == 201, resp.json()
    nima = User.objects.get(username="nima")
    assert nima.check_password(STRONG) and not nima.is_staff
    assert ChangeLog.objects.filter(
        model_label="auth.user", object_pk=str(nima.pk), action="add"
    ).exists()


@pytest.mark.parametrize(
    "p1, p2, field",
    [
        (STRONG, STRONG + "x", "password2"),  # mismatch
        ("12345678", "12345678", "password2"),  # numeric and common
        ("nima-user", "nima-user", "password2"),  # too similar to the username
    ],
)
def test_add_user_validates_passwords(admin_client, p1, p2, field):
    resp = admin_client.post(
        crud("auth.user"),
        {"username": "nima-user", "usable_password": "true", "password1": p1, "password2": p2},
        "application/json",
    )
    assert resp.status_code == 400
    assert field in resp.json()["errors"]
    assert not User.objects.filter(username="nima-user").exists()


def test_add_user_without_password_login(admin_client):
    resp = admin_client.post(
        crud("auth.user"), {"username": "sso", "usable_password": "false"}, "application/json"
    )
    assert resp.status_code == 201, resp.json()
    assert not User.objects.get(username="sso").has_usable_password()


def test_change_form_shows_password_status_never_the_hash(admin_client, django_user_model):
    user = django_user_model.objects.create_user("show", password=STRONG)
    data = admin_client.get(crud("auth.user", user.pk)).json()
    assert data["values"]["password"] is None
    assert data["password_status"] == {"usable": True, "algorithm": "md5"}  # test hasher
    assert user.password not in str(data)


def test_change_password_page(admin_client, django_user_model):
    user = django_user_model.objects.create_user("pw", password=STRONG)
    form = admin_client.get(crud("auth.user", "password-form")).json()
    assert [f["name"] for f in form["fields"]] == ["usable_password", "password1", "password2"]
    assert form["help"]

    url = crud("auth.user", f"{user.pk}/password")
    bad = admin_client.post(url, {"password1": "a", "password2": "b"}, "application/json")
    assert bad.status_code == 400 and "password2" in bad.json()["errors"]
    weak = admin_client.post(url, {"password1": "pw", "password2": "pw"}, "application/json")
    assert weak.status_code == 400
    ok = admin_client.post(
        url,
        {
            "usable_password": "true",
            "password1": "New-Strong-Pass-77",
            "password2": "New-Strong-Pass-77",
        },
        "application/json",
    )
    assert ok.status_code == 200, ok.json()
    user.refresh_from_db()
    assert user.check_password("New-Strong-Pass-77")
    entry = ChangeLog.objects.get(model_label="auth.user", object_pk=str(user.pk))
    assert entry.changes == {"fields": ["password"]}

    off = admin_client.post(url, {"usable_password": "false"}, "application/json")
    assert off.status_code == 200
    user.refresh_from_db()
    assert not user.has_usable_password()
    assert admin_client.post(
        crud("shop.city", "1/password"), {}, "application/json"
    ).status_code in (404,)


def test_changing_your_own_password_keeps_you_logged_in(client, django_user_model):
    me = django_user_model.objects.create_superuser("me", "me@example.com", STRONG)
    client.force_login(me)
    resp = client.post(
        crud("auth.user", f"{me.pk}/password"),
        {
            "usable_password": "true",
            "password1": "Another-Strong-1",
            "password2": "Another-Strong-1",
        },
        "application/json",
    )
    assert resp.status_code == 200
    assert client.get(f"{API}/auth/me").json()["authenticated"] is True


def test_password_page_needs_change_permission_and_scope(make_staff, django_user_model):
    target = django_user_model.objects.create_user("target", password=STRONG)
    viewer = login(make_staff("auth.view_user"))
    resp = viewer.post(
        crud("auth.user", f"{target.pk}/password"),
        {"password1": "Another-Strong-1", "password2": "Another-Strong-1"},
        "application/json",
    )
    assert resp.status_code == 403
    target.refresh_from_db()
    assert target.check_password(STRONG)


def test_only_superusers_manage_superusers(make_staff, django_user_model):
    boss = django_user_model.objects.create_superuser("boss", "b@example.com", STRONG)
    staff = make_staff("auth.view_user", "auth.change_user", "auth.delete_user")
    client = login(staff)
    for resp in (
        client.patch(crud("auth.user", boss.pk), {"first_name": "Pwned"}, "application/json"),
        client.post(
            crud("auth.user", f"{boss.pk}/password"),
            {"password1": "Another-Strong-1", "password2": "Another-Strong-1"},
            "application/json",
        ),
        client.delete(crud("auth.user", boss.pk)),
        client.get(crud("auth.user", f"{boss.pk}/delete-plan")),
    ):
        assert resp.status_code == 403
    boss.refresh_from_db()
    assert boss.first_name == "" and boss.check_password(STRONG)
    # ordinary accounts are fine
    other = django_user_model.objects.create_user("other", is_staff=True)
    assert (
        client.patch(
            crud("auth.user", other.pk), {"first_name": "Ok"}, "application/json"
        ).status_code
        == 200
    )


def test_staff_cant_grant_what_they_dont_have(make_staff, django_user_model):
    staff = make_staff("auth.view_user", "auth.change_user", "shop.view_city")
    client = login(staff)
    other = django_user_model.objects.create_user("other", is_staff=True)
    url = crud("auth.user", other.pk)

    resp = client.patch(url, {"is_superuser": True}, "application/json")
    assert resp.status_code == 400 and "is_superuser" in resp.json()["errors"]
    resp = client.patch(url, {"user_permissions": perms("delete_city")}, "application/json")
    assert (
        resp.status_code == 400
        and "shop.delete_city" in resp.json()["errors"]["user_permissions"][0]
    )
    resp = client.patch(url, {"user_permissions": perms("view_city")}, "application/json")
    assert resp.status_code == 200, resp.json()

    powerful = Group.objects.create(name="Powerful")
    powerful.permissions.set(perms("delete_order"))
    harmless = Group.objects.create(name="Harmless")
    harmless.permissions.set(perms("view_city"))
    resp = client.patch(url, {"groups": [powerful.pk]}, "application/json")
    assert resp.status_code == 400 and "groups" in resp.json()["errors"]
    assert client.patch(url, {"groups": [harmless.pk]}, "application/json").status_code == 200
    other.refresh_from_db()
    assert not other.is_superuser and list(other.groups.all()) == [harmless]

    # superusers may grant anything
    root = django_user_model.objects.create_superuser("root2", "r@example.com", STRONG)
    assert (
        login(root)
        .patch(url, {"groups": [powerful.pk], "is_superuser": True}, "application/json")
        .status_code
        == 200
    )


def test_group_permissions_are_guarded_too(make_staff):
    staff = make_staff("auth.view_group", "auth.change_group", "auth.add_group", "shop.view_city")
    client = login(staff)
    resp = client.post(
        crud("auth.group"),
        {"name": "Escalate", "permissions": perms("delete_city")},
        "application/json",
    )
    assert resp.status_code == 400
    resp = client.post(
        crud("auth.group"), {"name": "Fine", "permissions": perms("view_city")}, "application/json"
    )
    assert resp.status_code == 201


def test_permission_tree(admin_client):
    tree = admin_client.get(
        crud("auth.user", "permission-tree"), {"field": "user_permissions"}
    ).json()
    apps = {a["app_label"]: a for a in tree["apps"]}
    shop = {m["model"]: m for m in apps["shop"]["models"]}
    order = shop["shop.order"]
    assert order["verbose_name"] == "Orders"
    assert [p["label"] for p in order["permissions"]] == ["View", "Add", "Change", "Delete"]
    assert order["permissions"][0]["codename"] == "shop.view_order"
    system = {m["model"]: m for m in apps["bi_admin"]["models"]}["bi_admin.systemdashboard"]
    assert system["permissions"][0]["label"] == "Can view the system dashboard"
    assert system["permissions"][0]["action"] is None

    group_tree = admin_client.get(crud("auth.group", "permission-tree"), {"field": "permissions"})
    assert group_tree.status_code == 200
    assert (
        admin_client.get(crud("auth.user", "permission-tree"), {"field": "groups"}).status_code
        == 400
    )


def test_permission_tree_in_persian(admin_client, admin_user):
    from bi_admin.models import UserPreferences

    UserPreferences.objects.create(user=admin_user, language="fa")
    tree = admin_client.get(
        crud("auth.user", "permission-tree"), {"field": "user_permissions"}
    ).json()
    shop = next(a for a in tree["apps"] if a["app_label"] == "shop")
    order = next(m for m in shop["models"] if m["model"] == "shop.order")
    assert order["verbose_name"] == "سفارش‌ها"
    assert order["permissions"][0]["label"] == "مشاهده"


def test_dual_list_can_fetch_many_groups(admin_client):
    for i in range(30):
        Group.objects.create(name=f"G{i:02d}")
    resp = admin_client.get(crud("auth.user", "autocomplete"), {"field": "groups", "limit": 100})
    assert len(resp.json()["results"]) == 30 and resp.json()["more"] is False
    resp = admin_client.get(
        crud("auth.user", "autocomplete"), {"field": "user_permissions", "q": "order"}
    )
    assert resp.status_code == 200 and resp.json()["results"]
