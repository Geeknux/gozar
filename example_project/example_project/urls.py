from django.urls import include, path

urlpatterns = [
    path("gozar/", include("bi_admin.urls")),
]
