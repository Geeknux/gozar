"""Serves the React SPA shell; all data flows through the JSON API."""

import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.shortcuts import render
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie

from bi_admin import preferences

SPA_DIR = Path(__file__).resolve().parent / "static" / "bi_admin" / "spa"
MANIFEST = SPA_DIR / ".vite" / "manifest.json"
ENTRY = "index.html"


@lru_cache(maxsize=1)
def _manifest_cached(mtime: float) -> dict:
    return json.loads(MANIFEST.read_text())


def spa_assets() -> dict | None:
    """Script/CSS URLs of the built SPA, or ``None`` when it is not built."""
    if not MANIFEST.exists():
        return None
    entry = _manifest_cached(MANIFEST.stat().st_mtime).get(ENTRY)
    if not entry:
        return None
    return {
        "js": static(f"bi_admin/spa/{entry['file']}"),
        "css": [static(f"bi_admin/spa/{c}") for c in entry.get("css", [])],
    }


@ensure_csrf_cookie
def spa(request, path: str = ""):
    match = request.resolver_match  # the instance namespace this mount resolved under
    base = reverse(f"{match.namespace}:spa" if match and match.namespace else "bi_admin:spa")
    prefs = preferences.get(request)
    language = getattr(request, "LANGUAGE_CODE", None) or prefs.language
    if language not in preferences.LANGUAGES:
        language = preferences.default_language()
    config = {
        "basePath": base.rstrip("/"),
        "apiBase": base + "api",
        "language": language,
        "calendar": prefs.calendar,
        "defaultLanguage": preferences.default_language(),
        "defaultCalendar": preferences.default_calendar(),
    }
    dev_server = getattr(settings, "GOZAR", {}).get("VITE_DEV_SERVER")
    return render(
        request,
        "bi_admin/index.html",
        {
            "config": config,
            "dir": "rtl" if language == "fa" else "ltr",
            "assets": None if dev_server else spa_assets(),
            "dev_server": dev_server.rstrip("/") if dev_server else None,
        },
    )
