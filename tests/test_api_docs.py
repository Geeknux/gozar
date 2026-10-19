"""The Swagger docs page and the OpenAPI schema work wherever bi_admin.urls is
mounted (the default namespace, a custom instance namespace, nested), staff only
unless GOZAR["API_DOCS"] = "public"."""

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db

MOUNTS = [
    pytest.param("/gozar/api", None, id="default"),
    pytest.param("/custom/api", "urls_custom", id="instance-namespace"),
    pytest.param("/project/bi/api", "urls_custom", id="nested-namespace"),
]


@pytest.fixture
def staff(make_staff):
    client = Client()
    client.force_login(make_staff())  # no model permissions needed to read the docs
    return client


@pytest.mark.parametrize("base, urlconf", MOUNTS)
def test_docs_and_schema_for_staff(staff, settings, base, urlconf):
    if urlconf:
        settings.ROOT_URLCONF = urlconf
    page = staff.get(f"{base}/docs")
    assert page.status_code == 200
    assert f'"url": "{base}/openapi.json"'.encode() in page.content
    assert b"/static/bi_admin/favicon.svg" in page.content
    assert b"django-ninja.dev" not in page.content  # no hot-linked third-party favicon
    assert b"X-CSRFToken" in page.content  # "Try it out" works with the session
    schema = staff.get(f"{base}/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert f"{base}/reports/run" in paths and f"{base}/crud/{{app_label}}/{{model_name}}/" in paths


@pytest.mark.parametrize("base, urlconf", MOUNTS)
def test_anonymous_users_are_sent_to_the_login(settings, base, urlconf):
    if urlconf:
        settings.ROOT_URLCONF = urlconf
    page = Client().get(f"{base}/docs")
    spa = base.removesuffix("/api")
    assert page.status_code == 302
    assert page["Location"] == f"{spa}/login?next=%2F{base[1:].replace('/', '%2F')}%2Fdocs"
    assert Client().get(f"{base}/openapi.json").status_code == 401


def test_non_staff_users_are_refused(django_user_model):
    client = Client()
    client.force_login(django_user_model.objects.create_user("joe", password="pw-123456!"))
    assert client.get("/gozar/api/docs").status_code == 403
    assert client.get("/gozar/api/openapi.json").status_code == 403


def test_public_docs(settings):
    settings.GOZAR = {**settings.GOZAR, "API_DOCS": "public"}
    assert Client().get("/gozar/api/docs").status_code == 200
    assert Client().get("/gozar/api/openapi.json").status_code == 200
    # the API itself stays staff only
    assert Client().get("/gozar/api/meta/models").status_code == 401


@pytest.mark.parametrize("base, urlconf", MOUNTS)
def test_spa_shell_points_at_its_own_mount(staff, settings, base, urlconf):
    if urlconf:
        settings.ROOT_URLCONF = urlconf
    spa = base.removesuffix("/api")
    page = staff.get(f"{spa}/reports")
    assert page.status_code == 200
    assert f'"apiBase": "{base}"'.encode() in page.content


def test_spa_shell_links_a_favicon(staff):
    from django.contrib.staticfiles import finders

    page = staff.get("/gozar/")
    assert b'<link rel="icon" type="image/svg+xml" href="/static/bi_admin/favicon.svg">' in (
        page.content
    )
    assert finders.find("bi_admin/favicon.svg")
