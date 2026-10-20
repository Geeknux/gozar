from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

urlpatterns = [
    path("gozar/", include("bi_admin.urls")),
    path("shop/", include("shop.urls")),
]
# Uploaded files (product images, datasheets) in development; a web server or
# object storage serves MEDIA_URL in production.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
