from django.urls import path

from shop import views

app_name = "shop"
urlpatterns = [path("products/<int:pk>/", views.product, name="product")]
