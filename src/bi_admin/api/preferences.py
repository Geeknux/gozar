"""The current user's interface preferences."""

import json

from django.utils.translation import gettext as _
from ninja import Router

from bi_admin import preferences
from bi_admin.crud.service import CrudError

router = Router(tags=["preferences"])


@router.get("")
def get_preferences(request):
    return preferences.get(request).as_dict()


@router.patch("")
def update_preferences(request):
    """Body: any of ``language`` (en|fa), ``calendar`` (gregorian|jalali),
    ``options`` (object, merged). ``null``/``""`` resets to the project default.
    The response is already in the newly chosen language."""
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        raise CrudError(400, _("Invalid JSON body")) from None
    if not isinstance(data, dict):
        raise CrudError(400, _("Request body must be a JSON object"))
    try:
        prefs = preferences.update(request, data)
    except preferences.PreferenceError as exc:
        raise CrudError(400, _("Validation failed"), errors=exc.errors) from None
    preferences.activate(request, prefs)
    return prefs.as_dict()
