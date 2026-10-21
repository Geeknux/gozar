"""Per-language names and descriptions of what Gozar itself stores.

Saved reports, dashboards, dashboard widgets and e-mail schedules keep their
text in two places: the plain field (``name``, ``description``, ``title``) holds
the project's default language (``GOZAR["DEFAULT_LANGUAGE"]``) and stays the
canonical value (ordering, ``__str__``, the Django admin), and ``<field>_i18n``
maps each configured language to its text, the default language included.
``Translatable.save()`` keeps the two in step.

Viewers get the text in their language, falling back to the default language,
then to the plain field, then to any language that has one.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q
from django.utils import translation
from django.utils.translation import gettext as _

from bi_admin.models import LANGUAGE_CHOICES

LANGUAGES = tuple(code for code, _name in LANGUAGE_CHOICES)
# Unicode directional isolates: display-only marks the SPA adds around user text,
# never part of a stored name
ISOLATES = dict.fromkeys(map(ord, "\u2066\u2067\u2068\u2069"))


def clean_text(value: str) -> str:
    return value.translate(ISOLATES).strip()


def default_language() -> str:
    from bi_admin import preferences

    return preferences.default_language()


def current_language() -> str:
    lang = (translation.get_language() or "").split("-")[0]
    return lang if lang in LANGUAGES else default_language()


def pick(values: dict[str, str] | None, plain: str = "", lang: str | None = None) -> str:
    """The text for ``lang`` (default: the active language) with the fallbacks."""
    values = values if isinstance(values, dict) else {}
    lang = lang or current_language()
    for candidate in (values.get(lang), values.get(default_language()), plain):
        if candidate:
            return candidate
    return next((values[code] for code in LANGUAGES if values.get(code)), "")


def text(obj, field: str, lang: str | None = None) -> str:
    """``obj.<field>`` in ``lang`` (default: the active language)."""
    return pick(getattr(obj, f"{field}_i18n", None), getattr(obj, field, "") or "", lang)


def translations(obj, field: str) -> dict[str, str]:
    """Every stored language of ``obj.<field>`` (for editing)."""
    values = getattr(obj, f"{field}_i18n", None) or {}
    return {lang: values[lang] for lang in LANGUAGES if values.get(lang)}


def clean(values: Any, field: str, max_length: int | None = None) -> dict[str, str]:
    """Validate a ``{language: text}`` mapping from the API: known languages only,
    stripped, empty texts dropped. Raises ``TranslationError``."""
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise TranslationError({field: [_("Must map language codes to texts.")]})
    errors = []
    out = {}
    for lang, value in values.items():
        if lang not in LANGUAGES:
            errors.append(_("Unknown language %(lang)r") % {"lang": lang})
            continue
        if value is None:
            continue
        if not isinstance(value, str):
            errors.append(_("Must map language codes to texts."))
            continue
        value = clean_text(value)
        if max_length and len(value) > max_length:
            errors.append(_("At most %(n)s characters.") % {"n": max_length})
        elif value:
            out[lang] = value
    if errors:
        raise TranslationError({field: errors})
    return out


def assign(obj, field: str, plain: str | None, values: dict[str, str] | None) -> None:
    """Set ``obj.<field>`` from the API's plain text and/or translations.

    Given translations replace the stored ones (a plain text fills the default
    language when they leave it out); a plain text alone sets the default
    language's text and keeps the others."""
    default = default_language()
    if values is not None:
        values = dict(values)
        if plain and not values.get(default):
            values[default] = plain
    elif plain is not None:
        values = {k: v for k, v in (getattr(obj, f"{field}_i18n", None) or {}).items()}
        values.pop(default, None)
        if plain:
            values[default] = plain
    else:
        return
    setattr(obj, f"{field}_i18n", values)
    setattr(obj, field, "")
    sync(obj, field)


def sync(obj, field: str) -> None:
    """Keep the plain field and the translations in step (``Translatable.save()``).

    A plain field changed directly (the ORM, Django's admin, an old client)
    becomes the default language's text. A blank one shows the default
    language's text, else the first language there is — then the default
    language stays without a translation of its own."""
    values = getattr(obj, f"{field}_i18n", None)
    values = {k: v for k, v in (values if isinstance(values, dict) else {}).items() if v}
    plain = getattr(obj, field, "") or ""
    default = default_language()
    current = pick(values, "", default)
    if plain and plain != current:
        values[default] = plain
    elif not plain:
        plain = current
    setattr(obj, field, plain)
    setattr(obj, f"{field}_i18n", {lang: values[lang] for lang in LANGUAGES if values.get(lang)})


def search_q(query: str, *fields: str) -> Q:
    """Match ``query`` in the plain fields and in every language's text (JSON is
    read per key, so non-Latin text matches on every database backend)."""
    q = Q()
    for field in fields:
        q |= Q(**{f"{field}__icontains": query})
        for lang in LANGUAGES:
            q |= Q(**{f"{field}_i18n__{lang}__icontains": query})
    return q


def copy_of(values: dict[str, str], max_length: int = 200) -> dict[str, str]:
    """ "Copy of …" for a duplicate's name, in each of its languages."""
    out = {}
    for lang, value in values.items():
        with translation.override(lang):
            out[lang] = (_("Copy of %(name)s") % {"name": value})[:max_length]
    return out


class TranslationError(ValueError):
    def __init__(self, errors: dict[str, list[str]]):
        super().__init__(str(errors))
        self.errors = errors
