"""Server-side display formatting of report results (scheduled e-mails).

The SPA formats with ``Intl``; e-mails are rendered on the server, so this module
mirrors the SPA's rules in Python: measures by their semantic ``format``, time
buckets by grain in the report's calendar (``Mehr 1405``, ``Q4 2026``), choice
labels, and Persian digits and separators for ``fa``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from django.utils.translation import gettext as _

from bi_admin.calendar import jalali
from bi_admin.nl.interpret import GREGORIAN_MONTHS_EN, GREGORIAN_MONTHS_FA, JALALI_MONTHS_EN
from bi_admin.nl.interpret import format_date as date
from bi_admin.nl.text import persian_digits

__all__ = ["bucket", "cell", "date", "isolate", "measure", "number"]

DECIMALS = {"currency": 0, "integer": 0, "decimal": 2, "number": 2}


FSI, PDI = "\u2068", "\u2069"  # first-strong isolate … pop directional isolate


def _strong_dirs(text: str) -> set[str]:
    import unicodedata

    dirs = set()
    for ch in text:
        kind = unicodedata.bidirectional(ch)
        if kind == "L":
            dirs.add("ltr")
        elif kind in ("R", "AL"):
            dirs.add("rtl")
        if len(dirs) == 2:
            break
    return dirs


def isolate(text: str, lang: str | None = None) -> str:
    """``text`` wrapped in Unicode isolates where its surroundings (an e-mail
    subject, plain text) could reorder it: when it holds left-to-right *and*
    right-to-left letters, or letters with neutral punctuation at an end and —
    given the surrounding ``lang`` — a direction other than that language's.
    Other text is returned as is."""
    if not text or (FSI in text and PDI in text):
        return text
    dirs = _strong_dirs(text)
    if not dirs:
        return text
    if len(dirs) == 2:
        return f"{FSI}{text}{PDI}"
    if lang is not None and dirs == {"rtl" if lang == "fa" else "ltr"}:
        return text
    if not all(_strong_dirs(ch) for ch in text[0] + text[-1]):
        return f"{FSI}{text}{PDI}"
    return text


def _localise(text: str, lang: str) -> str:
    if lang != "fa":
        return text
    return persian_digits(text.replace(",", "٬").replace(".", "٫"))


def number(value: Any, decimals: int = 2, lang: str = "en", fixed: bool = False) -> str:
    text = f"{float(value):,.{decimals}f}"
    if not fixed and "." in text:
        text = text.rstrip("0").rstrip(".")
    return _localise(text, lang)


def measure(value: Any, fmt: str | None, lang: str) -> str:
    """A measure value as the SPA shows it (``—`` for null)."""
    if value is None or value == "":
        return "—"
    if isinstance(value, bool) or not isinstance(value, int | float):
        return str(value)
    if fmt == "percent":
        text = number(value * 100, 1, lang)
        return f"{text}٪" if lang == "fa" else f"{text}%"
    if fmt == "decimal":
        return number(value, 2, lang, fixed=True)
    return number(value, DECIMALS.get(fmt or "number", 2), lang)


def signed(value: Any, fmt: str | None, lang: str) -> str:
    text = measure(value, fmt, lang)
    if isinstance(value, int | float) and value > 0:
        return "+" + text
    if isinstance(value, int | float) and value < 0:
        return "−" + text.lstrip("-")
    return text


def _month(day: dt.date, calendar: str, lang: str) -> tuple[str, int]:
    if calendar == "jalali":
        j = jalali.to_jalali(day)
        names = jalali.MONTH_NAMES_FA if lang == "fa" else JALALI_MONTHS_EN
        return names[j.month - 1], j.year
    names = GREGORIAN_MONTHS_FA if lang == "fa" else GREGORIAN_MONTHS_EN
    return names[day.month - 1], day.year


def bucket(iso: str, grain: str, calendar: str, lang: str) -> str:
    """A time bucket that starts on ``iso`` (``Mehr 1405``, ``Q4 2026``…)."""
    day = dt.date.fromisoformat(iso)
    if grain == "day":
        return date(day, calendar, lang)
    if grain == "week":
        return _("Week of %(date)s") % {"date": date(day, calendar, lang)}
    if grain == "month":
        name, year = _month(day, calendar, lang)
        return _localise(f"{name} {year}", lang)
    if grain == "season":
        if calendar == "jalali":
            j = jalali.to_jalali(day)
            season = jalali.SEASON_NAMES[lang][jalali.season_of(j.month)]
            return _localise(f"{season} {j.year}", lang)
        quarter = (day.month - 1) // 3 + 1
        return _localise(_("Q%(quarter)s %(year)s") % {"quarter": quarter, "year": day.year}, lang)
    year = jalali.to_jalali(day).year if calendar == "jalali" else day.year
    return _localise(str(year), lang)


def cell(column: dict, row: dict, lang: str) -> str:
    """Display text of ``column`` in a result ``row``."""
    value = row.get(column["key"])
    if column["role"] != "dimension":
        return measure(value, column.get("format"), lang)
    if value is None:
        return _("(empty)")
    if column.get("grain"):
        return bucket(value, column["grain"], column.get("calendar") or "gregorian", lang)
    for choice in column.get("choices") or []:
        if choice["value"] == value:
            return str(choice["label"])
    if isinstance(value, bool):
        return _("Yes") if value else _("No")
    if isinstance(value, int | float):
        return number(value, 2, lang)
    return _localise(str(value), lang) if lang == "fa" and str(value).isdigit() else str(value)
