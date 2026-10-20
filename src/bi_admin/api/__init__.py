"""JSON API (django-ninja), mounted at ``<prefix>/api/``."""

import bi_admin
from bi_admin.api.audit import router as audit_router
from bi_admin.api.auth import router as auth_router
from bi_admin.api.crud import router as crud_router
from bi_admin.api.crud import search_router, views_router
from bi_admin.api.dashboards import router as dashboards_router
from bi_admin.api.docs import GozarAPI, GozarSwagger, docs_access
from bi_admin.api.meta import router as meta_router
from bi_admin.api.nl import router as nl_router
from bi_admin.api.preferences import router as preferences_router
from bi_admin.api.reports import router as reports_router
from bi_admin.api.saved import router as saved_router
from bi_admin.api.saved import sharing_router
from bi_admin.api.schedules import router as schedules_router
from bi_admin.api.security import staff_auth
from bi_admin.crud.service import CrudError
from bi_admin.engine import ReportError, SpecValidationError

api = GozarAPI(
    title="Gozar",
    version=bi_admin.__version__,
    urls_namespace="bi_admin_api",
    auth=staff_auth,
    docs=GozarSwagger(),
    docs_decorator=docs_access,  # staff only unless GOZAR["API_DOCS"] = "public"
)


@api.exception_handler(CrudError)
def crud_error(request, exc: CrudError):
    return api.create_response(request, exc.payload, status=exc.status)


@api.exception_handler(SpecValidationError)
def spec_error(request, exc: SpecValidationError):
    return api.create_response(request, exc.as_dict(), status=422)


@api.exception_handler(ReportError)
def report_error(request, exc: ReportError):
    return api.create_response(request, exc.payload, status=exc.status)


@api.get("/health", auth=None)
def health(request):
    return {"status": "ok", "version": bi_admin.__version__}


api.add_router("/auth", auth_router)
api.add_router("/meta", meta_router)
api.add_router("/crud", crud_router)
api.add_router("/list-views", views_router)
api.add_router("/search", search_router)
api.add_router("/reports", reports_router)
api.add_router("/preferences", preferences_router)
api.add_router("/audit", audit_router)
api.add_router("/nl", nl_router)
api.add_router("/saved-reports", saved_router)
api.add_router("/dashboards", dashboards_router)
api.add_router("/schedules", schedules_router)
api.add_router("/sharing", sharing_router)
