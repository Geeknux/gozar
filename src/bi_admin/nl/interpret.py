"""A human-readable sentence for a validated report, in the user's language:

    Revenue by City, monthly (Jalali), the last 3 months (1 Tir – 31 Shahrivar 1405),
    where Status is Paid, top 5 by Revenue.

It is built from the *validated* report (resolved period, coerced values,
choice labels), so it describes exactly what will run — the NL interface shows
it with every answer and the report builder with every spec.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from django.utils import translation
from django.utils.translation import gettext as _
from django.utils.translation import pgettext

from bi_admin.calendar import jalali, periods
from bi_admin.engine.validator import ValidatedReport
from bi_admin.nl.text import persian_digits

JALALI_MONTHS_EN = [
    "Farvardin",
    "Ordibehesht",
    "Khordad",
    "Tir",
    "Mordad",
    "Shahrivar",
    "Mehr",
    "Aban",
    "Azar",
    "Dey",
    "Bahman",
    "Esfand",
]
GREGORIAN_MONTHS_EN = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]
GREGORIAN_MONTHS_FA = [
    "ژانویه",
    "فوریه",
    "مارس",
    "آوریل",
    "مه",
    "ژوئن",
    "ژوئیه",
    "اوت",
    "سپتامبر",
    "اکتبر",
    "نوامبر",
    "دسامبر",
]


def _digits(value: str, lang: str) -> str:
    return persian_digits(value) if lang == "fa" else value


def format_date(day: dt.date, calendar: str, lang: str) -> str:
    if calendar == "jalali":
        j = jalali.to_jalali(day)
        names = jalali.MONTH_NAMES_FA if lang == "fa" else JALALI_MONTHS_EN
        return _digits(f"{j.day} {names[j.month - 1]} {j.year}", lang)
    if lang == "fa":
        return _digits(f"{day.day} {GREGORIAN_MONTHS_FA[day.month - 1]} {day.year}", lang)
    return f"{GREGORIAN_MONTHS_EN[day.month - 1]} {day.day}, {day.year}"


def format_number(value: Any, lang: str) -> str:
    if isinstance(value, Decimal):
        value = int(value) if value == value.to_integral_value() else float(value)
    text = f"{value:,}" if isinstance(value, int | float) else str(value)
    return persian_digits(text.replace(",", "٬")) if lang == "fa" else text


def _join(items: list[str], conj: str) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    comma = _(", ")
    return f"{comma.join(items[:-1])} {conj} {items[-1]}"


def _unit(unit: str, calendar: str, plural: bool = False) -> str:
    if unit == "season" and calendar == "gregorian":
        unit = "quarter"
    singular = {
        "day": pgettext("period unit", "day"),
        "week": pgettext("period unit", "week"),
        "month": pgettext("period unit", "month"),
        "season": pgettext("period unit", "season"),
        "quarter": pgettext("period unit", "quarter"),
        "year": pgettext("period unit", "year"),
    }
    many = {
        "day": pgettext("period unit plural", "days"),
        "week": pgettext("period unit plural", "weeks"),
        "month": pgettext("period unit plural", "months"),
        "season": pgettext("period unit plural", "seasons"),
        "quarter": pgettext("period unit plural", "quarters"),
        "year": pgettext("period unit plural", "years"),
    }
    return (many if plural else singular)[unit]


def describe_period(period: periods.Period, lang: str) -> str:
    kind, count, unit = periods.parse(period.name)
    name = period.name.strip().lower()
    cal = period.calendar
    if name == "today":
        text = _("today")
    elif name == "yesterday":
        text = _("yesterday")
    elif kind == "this":
        text = _("this %(unit)s") % {"unit": _unit(unit, cal)}
    elif kind == "last" and count == 1:
        text = _("last %(unit)s") % {"unit": _unit(unit, cal)}
    elif kind == "last":
        text = _("the last %(count)s %(units)s") % {
            "count": _digits(str(count), lang),
            "units": _unit(unit, cal, plural=True),
            "unit": _unit(unit, cal),
        }
    else:
        text = _("the past %(count)s %(units)s, including this %(unit)s") % {
            "count": _digits(str(count), lang),
            "units": _unit(unit, cal, plural=True),
            "unit": _unit(unit, cal),
        }
    start, end = format_date(period.start, cal, lang), format_date(period.end, cal, lang)
    span = (
        start
        if period.start == period.end
        else _("%(start)s – %(end)s")
        % {
            "start": start,
            "end": end,
        }
    )
    return f"{text} ({span})"


GRAINS = {
    "day": lambda: _("daily"),
    "week": lambda: _("weekly"),
    "month": lambda: _("monthly"),
    "season": lambda: _("by season"),
    "year": lambda: _("yearly"),
}


def _grain(grain: str, calendar: str) -> str:
    if grain == "season" and calendar == "gregorian":
        return _("quarterly")
    return GRAINS[grain]()


def _value(dim, value: Any, calendar: str, lang: str) -> str:
    if isinstance(value, dt.date):
        return format_date(value, calendar, lang)
    if isinstance(value, bool):
        return _("yes") if value else _("no")
    if dim.choices:
        for v, labels in dim.choices:
            if v == value:
                return labels.get(lang) or str(v)
    if isinstance(value, int | float | Decimal):
        return format_number(value, lang)
    return _digits(str(value), lang) if lang == "fa" else str(value)


def _filter(f, calendar: str, lang: str) -> str:
    dim = f.dimension
    label = dim.label.get(lang) or dim.key
    op, value = f.op, f.value
    if op == "is_null":
        return (_("%(field)s is empty") if value else _("%(field)s is not empty")) % {
            "field": label
        }
    if op in ("in", "not_in", "between"):
        values = [_value(dim, v, calendar, lang) for v in value]
    else:
        values = [_value(dim, value, calendar, lang)]
    params = {"field": label, "value": values[0]}
    if op == "eq":
        return _("%(field)s is %(value)s") % params
    if op == "neq":
        return _("%(field)s is not %(value)s") % params
    if op == "in":
        return _("%(field)s is %(values)s") % {"field": label, "values": _join(values, _("or"))}
    if op == "not_in":
        return _("%(field)s is neither %(values)s") % {
            "field": label,
            "values": _join(values, _("nor")),
        }
    if op == "gte":
        if dim.type == "time":
            return _("%(field)s from %(value)s") % params
        return _("%(field)s at least %(value)s") % params
    if op == "lte":
        if dim.type == "time":
            return _("%(field)s until %(value)s") % params
        return _("%(field)s at most %(value)s") % params
    if op == "between":
        if dim.type == "time":
            return _("%(field)s from %(low)s to %(high)s") % {
                "field": label,
                "low": values[0],
                "high": values[1],
            }
        return _("%(field)s between %(low)s and %(high)s") % {
            "field": label,
            "low": values[0],
            "high": values[1],
        }
    return _("%(field)s contains “%(value)s”") % params


def describe(report: ValidatedReport, lang: str | None = None, default_limit: int = 1000) -> str:
    """The interpretation sentence of ``report`` in ``lang`` (default: active)."""
    lang = lang or (translation.get_language() or "en").split("-")[0]
    lang = lang if lang in ("en", "fa") else "en"
    with translation.override(lang):
        return _describe(report, lang, default_limit)


def _describe(report: ValidatedReport, lang: str, default_limit: int) -> str:
    sem = report.semantic
    cal = report.calendar

    def label(item) -> str:
        return item.label.get(lang) or item.key

    measures = _join([label(m) for m in report.measures], _("and"))
    text = measures or (sem.label.get(lang) or sem.name)
    groups = [g for g in report.groups if not g.grain]
    if groups:
        text = _("%(measures)s by %(dimensions)s") % {
            "measures": text,
            "dimensions": _join([label(g.dimension) for g in groups], _("and")),
        }
    parts = [text]
    time_dim = report.time_dimension
    timed = [g for g in report.groups if g.grain]
    for g in timed:
        grain = _grain(g.grain, cal)
        cal_name = _("Jalali") if cal == "jalali" else _("Gregorian")
        piece = f"{grain} ({cal_name})"
        if time_dim is not None and (
            g.dimension is not time_dim or sem.time_dimension != time_dim.key
        ):
            piece = _("%(grain)s by %(field)s") % {"grain": piece, "field": label(g.dimension)}
        parts.append(piece)
    if report.period is not None:
        piece = describe_period(report.period, lang)
        if time_dim is not None and sem.time_dimension != time_dim.key and not timed:
            piece = _("%(period)s by %(field)s") % {"period": piece, "field": label(time_dim)}
        parts.append(piece)
    if report.comparison is not None:
        c = report.comparison
        start, end = format_date(c.start, cal, lang), format_date(c.end, cal, lang)
        span = (
            start if c.start == c.end else _("%(start)s – %(end)s") % {"start": start, "end": end}
        )
        if c.kind == "previous_period":
            parts.append(_("compared with the previous period (%(span)s)") % {"span": span})
        else:
            parts.append(_("compared with the same period last year (%(span)s)") % {"span": span})
    if report.filters:
        conditions = _join([_filter(f, cal, lang) for f in report.filters], _("and"))
        parts.append(_("where %(conditions)s") % {"conditions": conditions})
    spec = report.spec
    limited = spec.limit is not None and spec.limit != default_limit
    if report.sort:
        key, direction = report.sort[0]
        item = sem.measures.get(key) or sem.dimensions.get(key)
        name = label(item) if item else key
        if limited:
            n = _digits(str(report.limit), lang)
            template = (
                _("top %(n)s by %(field)s")
                if direction == "desc"
                else _("bottom %(n)s by %(field)s")
            )
            parts.append(template % {"n": n, "field": name})
        else:
            template = (
                _("sorted by %(field)s, highest first")
                if direction == "desc"
                else _("sorted by %(field)s, lowest first")
            )
            parts.append(template % {"field": name})
    elif limited:
        parts.append(_("first %(n)s rows") % {"n": _digits(str(report.limit), lang)})
    return _(", ").join(parts) + _(".")
