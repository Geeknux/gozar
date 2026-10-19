"""The API docs (Swagger UI at ``<api>/docs``, schema at ``<api>/openapi.json``).

django-ninja reverses its own URLs as ``"<urls_namespace>:openapi-json"``, which
breaks once ``bi_admin.urls`` (``app_name = "bi_admin"``) nests the API as
``bi_admin:bi_admin_api`` — or under any instance namespace a project picks.
Here every reverse uses the namespace the current request actually resolved
under, so the docs work wherever the URLs are mounted.

Access: ``GOZAR["API_DOCS"]`` = ``"staff"`` (default: active staff only, like
the API itself; anonymous visitors of the page are sent to the login) or
``"public"``.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from functools import wraps
from urllib.parse import urlencode

from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from ninja import NinjaAPI, Swagger

from bi_admin.conf import setting

_namespace: ContextVar[str | None] = ContextVar("gozar_api_namespace", default=None)


def _current_namespace(api: NinjaAPI) -> str:
    return _namespace.get() or api.urls_namespace


class GozarAPI(NinjaAPI):
    def get_root_path(self, path_params) -> str:
        return reverse(f"{_current_namespace(self)}:api-root", kwargs=path_params)


class GozarSwagger(Swagger):
    """Swagger UI from the CDN, with Gozar's own favicon (ninja's is hot-linked
    from its website, which browsers block)."""

    template = "bi_admin/api_docs.html"

    def get_openapi_url(self, api, path_params) -> str:
        return reverse(f"{_current_namespace(api)}:openapi-json", kwargs=path_params)

    def render_page(self, request, api, **kwargs):
        self.settings["url"] = self.get_openapi_url(api, kwargs)
        context = {
            "swagger_settings": json.dumps(self.settings, indent=1),
            "api": api,
            "add_csrf": True,  # the API uses session auth with CSRF
        }
        return render(request, self.template, context)


def _staff(user) -> bool:
    return bool(user is not None and user.is_authenticated and user.is_active and user.is_staff)


def docs_access(view):
    """Gate the docs views and remember the namespace they were reached under."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if setting("API_DOCS") != "public" and not _staff(getattr(request, "user", None)):
            match = request.resolver_match
            if (
                match is not None
                and match.url_name == "openapi-view"
                and not (request.user.is_authenticated)
            ):
                parent = ":".join(match.namespaces[:-1])  # bi_admin.urls' own namespace
                spa = reverse(f"{parent}:spa") if parent else "/"
                login = f"{spa.rstrip('/')}/login?{urlencode({'next': request.get_full_path()})}"
                return HttpResponseRedirect(login)
            status = 403 if request.user.is_authenticated else 401
            return JsonResponse({"detail": _("Staff access is required.")}, status=status)
        match = request.resolver_match
        token = _namespace.set(match.namespace if match is not None else None)
        try:
            return view(request, *args, **kwargs)
        finally:
            _namespace.reset(token)

    return wrapped
