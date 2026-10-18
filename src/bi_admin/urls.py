from django.urls import path, re_path

from bi_admin import views
from bi_admin.api import api

app_name = "bi_admin"

urlpatterns = [
    path("api/", api.urls),
    path("", views.spa, name="spa"),
    re_path(r"^(?!api/)(?P<path>.+)$", views.spa, name="spa-route"),
]
