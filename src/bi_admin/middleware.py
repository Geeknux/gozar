"""Activates each request's language from the user's Gozar preferences.

Use it instead of (or after) Django's ``LocaleMiddleware``, below
``AuthenticationMiddleware``::

    MIDDLEWARE = [..., "django.contrib.auth.middleware.AuthenticationMiddleware",
                  "bi_admin.middleware.PreferencesMiddleware", ...]
"""

from django.utils import translation
from django.utils.cache import patch_vary_headers

from bi_admin import preferences


class PreferencesMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        previous = translation.get_language()
        try:
            lang = preferences.activate(request)
            response = self.get_response(request)
        finally:  # don't leak the language into whatever this thread runs next
            if previous:
                translation.activate(previous)
            else:
                translation.deactivate()
        patch_vary_headers(response, ("Accept-Language", "Cookie"))
        response.headers.setdefault("Content-Language", request.LANGUAGE_CODE or lang)
        return response
