"""Per-user interface preferences (language, calendar) and their defaults.

Resolution order: the user's stored choice → ``GOZAR["DEFAULT_LANGUAGE"]`` /
``GOZAR["DEFAULT_CALENDAR"]`` → English / Gregorian. Before login the language
follows the browser (``Accept-Language`` or the language cookie) when it is
supported, otherwise the project default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.utils import translation

from bi_admin.models import LANGUAGE_CHOICES, UserPreferences

LANGUAGES = dict(LANGUAGE_CHOICES)
CALENDARS = tuple(UserPreferences.Calendar.values)
FALLBACK_LANGUAGE = "en"
FALLBACK_CALENDAR = "gregorian"


def _config() -> dict[str, Any]:
    return getattr(settings, "GOZAR", {}) or {}


def default_language() -> str:
    lang = _config().get("DEFAULT_LANGUAGE") or FALLBACK_LANGUAGE
    return lang if lang in LANGUAGES else FALLBACK_LANGUAGE


def default_calendar() -> str:
    cal = _config().get("DEFAULT_CALENDAR") or FALLBACK_CALENDAR
    return cal if cal in CALENDARS else FALLBACK_CALENDAR


@dataclass
class Preferences:
    language: str
    calendar: str
    options: dict[str, Any] = field(default_factory=dict)
    stored_language: str = ""  # "" = follow the project default
    stored_calendar: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "calendar": self.calendar,
            "options": self.options,
            "stored": {"language": self.stored_language, "calendar": self.stored_calendar},
            "defaults": {"language": default_language(), "calendar": default_calendar()},
            "available": {
                "languages": [
                    {"value": code, "label": name, "dir": "rtl" if code == "fa" else "ltr"}
                    for code, name in LANGUAGE_CHOICES
                ],
                "calendars": [
                    {"value": v, "label": str(label)}
                    for v, label in UserPreferences.Calendar.choices
                ],
            },
        }


def _staff(user) -> bool:
    return bool(user is not None and user.is_authenticated and user.is_active and user.is_staff)


def for_user(user) -> Preferences:
    row = UserPreferences.objects.filter(user=user).first() if _staff(user) else None
    stored_lang = row.language if row and row.language in LANGUAGES else ""
    stored_cal = row.calendar if row and row.calendar in CALENDARS else ""
    return Preferences(
        language=stored_lang or default_language(),
        calendar=stored_cal or default_calendar(),
        options=dict(row.options) if row else {},
        stored_language=stored_lang,
        stored_calendar=stored_cal,
    )


def browser_language(request) -> str | None:
    """The supported language the browser asks for, if any."""
    asked = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME) or request.META.get(
        "HTTP_ACCEPT_LANGUAGE"
    )
    if not asked:
        return None
    lang = translation.get_language_from_request(request)
    lang = (lang or "").split("-")[0]
    return lang if lang in LANGUAGES else None


def get(request) -> Preferences:
    """Preferences for this request (cached on it); anonymous users get defaults
    with the browser's language when supported."""
    cached = getattr(request, "_gozar_preferences", None)
    if cached is not None:
        return cached
    user = getattr(request, "user", None)
    prefs = for_user(user)
    if not _staff(user):
        prefs.language = browser_language(request) or prefs.language
    request._gozar_preferences = prefs
    return prefs


def activate(request, prefs: Preferences | None = None) -> str:
    prefs = prefs or get(request)
    translation.activate(prefs.language)
    request.LANGUAGE_CODE = prefs.language
    return prefs.language


class PreferenceError(ValueError):
    def __init__(self, errors: dict[str, list[str]]):
        super().__init__(str(errors))
        self.errors = errors


def update(request, data: dict[str, Any]) -> Preferences:
    """Apply a partial update (``language``, ``calendar``, ``options``); ``""`` or
    ``None`` resets a field to the project default."""
    from django.utils.translation import gettext as _

    errors: dict[str, list[str]] = {}
    unknown = sorted(set(data) - {"language", "calendar", "options"})
    for key in unknown:
        errors[key] = [_("Unknown preference.")]
    lang = data.get("language", ...)
    cal = data.get("calendar", ...)
    opts = data.get("options", ...)
    if lang not in (..., None, "") and lang not in LANGUAGES:
        errors["language"] = [_("Choose one of: %(choices)s.") % {"choices": ", ".join(LANGUAGES)}]
    if cal not in (..., None, "") and cal not in CALENDARS:
        errors["calendar"] = [_("Choose one of: %(choices)s.") % {"choices": ", ".join(CALENDARS)}]
    if opts is not ... and not isinstance(opts, dict):
        errors["options"] = [_("Must be an object.")]
    if errors:
        raise PreferenceError(errors)
    row, _created = UserPreferences.objects.get_or_create(user=request.user)
    if lang is not ...:
        row.language = lang or ""
    if cal is not ...:
        row.calendar = cal or ""
    if opts is not ...:
        row.options = {**row.options, **opts}
    row.save()
    prefs = for_user(request.user)
    request._gozar_preferences = prefs
    return prefs
