"""bi_admin.urls mounted twice: under a custom instance namespace and nested in a
project namespace (for the API docs tests)."""

from django.urls import include, path

nested = ([path("bi/", include("bi_admin.urls", namespace="inner"))], "outer")

urlpatterns = [
    path("custom/", include("bi_admin.urls", namespace="reports_admin")),
    path("project/", include(nested, namespace="outer")),
]
