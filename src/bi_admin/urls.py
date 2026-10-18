from django.urls import path

from bi_admin.api import api

urlpatterns = [
    path("api/", api.urls),
]
