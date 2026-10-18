from io import StringIO

import pytest
from django.contrib.auth.models import Permission
from django.core.management import call_command

from bi_admin.crud import site
from bi_admin.discovery import reset_registry

API = "/gozar/api"


@pytest.fixture(autouse=True)
def _fast_hashing(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_registry()
    site.clear_cache()
    yield
    reset_registry()
    site.clear_cache()


@pytest.fixture
def seeded(db):
    call_command("seed_demo", customers=20, orders=60, seed=7, stdout=StringIO())


@pytest.fixture
def admin_user(django_user_model):
    return django_user_model.objects.create_superuser("root", "root@example.com", "s3cret-pass!")


@pytest.fixture
def admin_client(client, admin_user):
    client.force_login(admin_user)
    return client


@pytest.fixture
def make_staff(django_user_model):
    def make(*perms: str, username="staff"):
        user = django_user_model.objects.create_user(
            username, password="x-pass-123!", is_staff=True
        )
        for perm in perms:
            app_label, codename = perm.split(".")
            user.user_permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        return user

    return make
