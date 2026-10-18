from django.contrib.auth import authenticate, login, logout
from django.middleware.csrf import get_token
from django.utils.translation import gettext as _
from ninja import Router, Schema
from ninja.errors import HttpError
from ninja.utils import check_csrf

from bi_admin import preferences

router = Router(tags=["auth"])


class LoginIn(Schema):
    username: str
    password: str


def user_payload(user) -> dict:
    return {
        "username": user.get_username(),
        "full_name": user.get_full_name() if hasattr(user, "get_full_name") else "",
        "is_superuser": user.is_superuser,
    }


@router.get("/csrf", auth=None)
def csrf(request):
    """Sets the CSRF cookie; the SPA calls this before its first POST."""
    return {"csrftoken": get_token(request)}


@router.get("/me", auth=None)
def me(request):
    user = request.user
    prefs = preferences.get(request).as_dict()
    if user.is_authenticated and user.is_active and user.is_staff:
        return {"authenticated": True, "user": user_payload(user), "preferences": prefs}
    return {"authenticated": False, "user": None, "preferences": prefs}


@router.post("/login", auth=None)
def do_login(request, data: LoginIn):
    error = check_csrf(request)
    if error:
        raise HttpError(403, _("CSRF verification failed"))
    user = authenticate(request, username=data.username, password=data.password)
    if user is None or not (user.is_active and user.is_staff):
        raise HttpError(
            401, _("Incorrect username or password, or the account has no admin access.")
        )
    login(request, user)
    request._gozar_preferences = None
    prefs = preferences.get(request)
    preferences.activate(request, prefs)
    return {
        "authenticated": True,
        "user": user_payload(user),
        "preferences": prefs.as_dict(),
        "csrftoken": get_token(request),
    }


@router.post("/logout")
def do_logout(request):
    logout(request)
    return {"authenticated": False}
