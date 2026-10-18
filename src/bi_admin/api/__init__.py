"""JSON API (django-ninja)."""

from ninja import NinjaAPI

import bi_admin

api = NinjaAPI(title="Gozar", version=bi_admin.__version__, urls_namespace="bi_admin_api")


@api.get("/health")
def health(request):
    return {"status": "ok", "version": bi_admin.__version__}
