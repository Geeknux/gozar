"""Deterministic, offline interpreter for common report questions (English + Persian).

Used when no LLM is configured (and as a fallback when it can't be reached). It
recognises, in this order: explicit dates, years and months; relative periods;
number comparisons; top/bottom N and sort order; time grain; chart type; then
the catalog's own words (measure/dimension/model labels, synonyms, choice
labels) and finally category values, found through the server-side value index
(scoped to the user). Follow-ups edit the previous spec instead of starting
over. Anything it can't settle becomes ``clarification_needed``.

It never guesses dates: periods are emitted as names and resolved by
``bi_admin.calendar.periods`` like any other spec.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from django.utils.translation import gettext as _

from bi_admin.nl import dates
from bi_admin.nl.catalog import Hit, ModelEntry, find_phrases, token_matches
from bi_admin.nl.providers import Generation, Provider, Task
from bi_admin.nl.text import normalise, tokens

# --- lexicons (written naturally; matched after normalisation) -----------------------

NUMBER_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "sixty": 60,
    "ninety": 90,
    "یک": 1,
    "دو": 2,
    "سه": 3,
    "چهار": 4,
    "پنج": 5,
    "شش": 6,
    "هفت": 7,
    "هشت": 8,
    "نه": 9,
    "ده": 10,
    "یازده": 11,
    "دوازده": 12,
    "پانزده": 15,
    "بیست": 20,
    "سی": 30,
    "شصت": 60,
    "نود": 90,
}
MULTIPLIERS = {
    "k": 1_000,
    "thousand": 1_000,
    "هزار": 1_000,
    "m": 1_000_000,
    "million": 1_000_000,
    "میلیون": 1_000_000,
    "b": 1_000_000_000,
    "billion": 1_000_000_000,
    "میلیارد": 1_000_000_000,
}
UNITS = {
    "day": "day",
    "days": "day",
    "روز": "day",
    "week": "week",
    "weeks": "week",
    "هفته": "week",
    "month": "month",
    "months": "month",
    "ماه": "month",
    "quarter": "season",
    "quarters": "season",
    "season": "season",
    "seasons": "season",
    "فصل": "season",
    "year": "year",
    "years": "year",
    "سال": "year",
}
THIS_WORDS = {"this", "current", "این"}
LAST_WORDS = {"last", "previous", "prior", "past"}
FA_LAST_AFTER = {"گذشته", "قبل", "پیش", "اخیر", "قبلی"}
FA_THIS_AFTER = {"جاری", "فعلی", "کنونی", "حاضر"}
FIXED_PERIODS = {
    "today": "today",
    "امروز": "today",
    "yesterday": "yesterday",
    "دیروز": "yesterday",
    "امسال": "this_year",
    "پارسال": "last_year",
    "ytd": "this_year",
    "year to date": "this_year",
    "mtd": "this_month",
    "month to date": "this_month",
    "so far this year": "this_year",
    "از اول سال": "this_year",
    "از ابتدای سال": "this_year",
    "از اول ماه": "this_month",
    "از ابتدای ماه": "this_month",
}
# Comparisons: a cue ("compared to", «نسبت به») followed by what to compare with.
# Payload: (compare_to kind, unit compared against or None).
_UNITS_EN = {"day": "day", "week": "week", "month": "month", "quarter": "season",
             "season": "season", "year": "year"}  # fmt: skip
_UNITS_FA = {"روز": "day", "هفته": "week", "ماه": "month", "فصل": "season", "سال": "year"}
_SPLY = "same_period_last_year"
_PREVIOUS = "previous_period"


def _compare_targets() -> dict[str, tuple[str, str | None]]:
    out: dict[str, tuple[str, str | None]] = {}
    for word, unit in _UNITS_EN.items():
        kind = _SPLY if unit == "year" else _PREVIOUS
        for before in ("last", "previous", "prior", "the last", "the previous", "the prior"):
            out[f"{before} {word}"] = (kind, unit)
        out[f"the {word} before"] = (kind, unit)
    for word, unit in _UNITS_FA.items():
        kind = _SPLY if unit == "year" else _PREVIOUS
        for after in ("قبل", "گذشته", "پیش", "قبلی"):
            out[f"{word} {after}"] = (kind, unit)
    out["پارسال"] = (_SPLY, "year")
    out["a year ago"] = (_SPLY, "year")
    for phrase in ("previous period", "prior period", "last period", "preceding period"):
        out[phrase] = out[f"the {phrase}"] = (_PREVIOUS, None)
    for word in ("دوره", "بازه", "مدت"):
        for after in ("قبل", "قبلی", "گذشته"):
            out[f"{word} {after}"] = (_PREVIOUS, None)
    for phrase in ("same period last year", "same time last year", "same period a year ago",
                   "same period of last year", "same period the year before"):  # fmt: skip
        out[phrase] = out[f"the {phrase}"] = (_SPLY, None)
    for word in ("دوره", "مدت", "بازه", "زمان"):
        for year in ("سال قبل", "سال گذشته", "پارسال", "سال پیش"):
            out[f"{word} مشابه {year}"] = (_SPLY, None)
            out[f"همین {word} {year}"] = (_SPLY, None)
            out[f"همین {word} در {year}"] = (_SPLY, None)
    return out


def _compare_phrases() -> dict[str, tuple[str, str | None]]:
    targets = _compare_targets()
    cues = [
        *("compared to", "compared with", "compare to", "compare with", "compare it to"),
        *("compare it with", "comparing to", "comparing with", "in comparison to"),
        *("in comparison with", "vs", "versus", "against", "relative to"),
        *("نسبت به", "در مقایسه با", "مقایسه با", "در قیاس با", "قیاس با"),
    ]
    out = {f"{cue} {target}": payload for cue in cues for target, payload in targets.items()}
    for target, payload in targets.items():  # «با سال قبل مقایسه کن»
        for verb in ("مقایسه", "مقایسه کن", "مقایسه شود", "مقایسه کنید"):
            out[f"با {target} {verb}"] = payload
    for phrase in ("year over year", "year-over-year", "yoy"):
        out[phrase] = (_SPLY, "year")
    for phrase, unit in (("month over month", "month"), ("week over week", "week"),
                         ("quarter over quarter", "season")):  # fmt: skip
        out[phrase] = (_PREVIOUS, unit)
    out["period over period"] = (_PREVIOUS, None)
    return out


COMPARE_PHRASES = _compare_phrases()
NO_COMPARE = [
    *("without comparison", "without the comparison", "no comparison", "remove comparison"),
    *("remove the comparison", "بدون مقایسه", "حذف مقایسه", "مقایسه را حذف کن"),
    "مقایسه رو حذف کن",
]

ALL_TIME = ["all time", "همه زمان ها", "کل دوره", "از ابتدا تا کنون", "بدون محدودیت زمانی"]

GRAINS = {
    "daily": "day",
    "by day": "day",
    "per day": "day",
    "each day": "day",
    "day by day": "day",
    "روزانه": "day",
    "روز به روز": "day",
    "به تفکیک روز": "day",
    "هر روز": "day",
    "weekly": "week",
    "by week": "week",
    "per week": "week",
    "each week": "week",
    "هفتگی": "week",
    "به تفکیک هفته": "week",
    "هر هفته": "week",
    "monthly": "month",
    "by month": "month",
    "per month": "month",
    "each month": "month",
    "month by month": "month",
    "month over month": "month",
    "ماهانه": "month",
    "ماهیانه": "month",
    "ماه به ماه": "month",
    "به تفکیک ماه": "month",
    "هر ماه": "month",
    "quarterly": "season",
    "by quarter": "season",
    "per quarter": "season",
    "by season": "season",
    "seasonal": "season",
    "فصلی": "season",
    "به تفکیک فصل": "season",
    "هر فصل": "season",
    "yearly": "year",
    "annual": "year",
    "annually": "year",
    "by year": "year",
    "per year": "year",
    "each year": "year",
    "year by year": "year",
    "سالانه": "year",
    "سالیانه": "year",
    "به تفکیک سال": "year",
    "هر سال": "year",
    "سال به سال": "year",
}
TREND_WORDS = ["trend", "over time", "روند", "طی زمان"]
CHARTS = {
    "pie": "pie",
    "pie chart": "pie",
    "نمودار دایره ای": "pie",
    "دایره ای": "pie",
    "نمودار کیکی": "pie",
    "bar chart": "bar",
    "bar": "bar",
    "bars": "bar",
    "column chart": "bar",
    "نمودار میله ای": "bar",
    "میله ای": "bar",
    "نمودار ستونی": "bar",
    "ستونی": "bar",
    "line chart": "line",
    "line": "line",
    "نمودار خطی": "line",
    "خطی": "line",
    "area chart": "area",
    "نمودار سطحی": "area",
    "table": "table",
    "as a table": "table",
    "جدول": "table",
    "جدولی": "table",
    "pivot": "pivot",
    "pivot table": "pivot",
    "جدول محوری": "pivot",
    "پیوت": "pivot",
    "kpi": "kpi",
    "کارت": "kpi",
}
TOP_DESC = [
    "top",
    "best",
    "highest",
    "most",
    "largest",
    "biggest",
    "leading",
    "best selling",
    "برتر",
    "برترین",
    "بیشترین",
    "بیش ترین",
    "بالاترین",
    "بهترین",
    "پرفروش",
    "پرفروشترین",
    "پرفروش ترین",
    "پر فروش ترین",
    "اول",
]
TOP_ASC = [
    "bottom",
    "lowest",
    "least",
    "worst",
    "smallest",
    "fewest",
    "کمترین",
    "کم ترین",
    "پایین ترین",
    "پایینترین",
    "بدترین",
    "کم فروش ترین",
    "کمفروشترین",
    "اخر",
]
# superlatives that imply a measure: the catalog phrase to look it up by
IMPLIED_MEASURE = {
    "best selling": "sales",
    "پرفروش": "فروش",
    "پرفروشترین": "فروش",
    "پرفروش ترین": "فروش",
    "پر فروش ترین": "فروش",
    "کم فروش ترین": "فروش",
    "کمفروشترین": "فروش",
}
ORDER_WORDS = {
    "ascending": "asc",
    "asc": "asc",
    "صعودی": "asc",
    "descending": "desc",
    "desc": "desc",
    "نزولی": "desc",
}
WHICH_WORDS = {"which", "کدام", "کدوم", "چه"}
GTE_WORDS = [
    "over",
    "above",
    "more than",
    "greater than",
    "at least",
    "bigger than",
    "higher than",
    "بیشتر از",
    "بیش از",
    "بالای",
    "بالاتر از",
    "حداقل",
    "بزرگتر از",
    "از",
]
LTE_WORDS = [
    "under",
    "below",
    "less than",
    "at most",
    "smaller than",
    "lower than",
    "کمتر از",
    "کم تر از",
    "زیر",
    "پایین تر از",
    "حداکثر",
    "کوچکتر از",
    "تا",
]
NEGATIONS = [
    "except",
    "excluding",
    "exclude",
    "not",
    "other than",
    "without",
    "به جز",
    "بجز",
    "جز",
    "غیر از",
    "به غیر از",
    "بغیر از",
    "منهای",
    "نه",
]
GROUP_CUES = {"by", "per", "each", "every", "across", "تفکیک", "اساس", "هر", "ازای"}
ADD_CUES = ["also", "add", "and also", "too", "as well", "plus", "همچنین", "اضافه", "هم"]
REMOVE_CUES = ["remove", "drop", "without", "حذف", "بدون"]
ALL_CUES = {"all", "every", "همه", "تمام", "کل", "تمامی"}
CALENDAR_WORDS = {
    "jalali": "jalali",
    "شمسی": "jalali",
    "خورشیدی": "jalali",
    "solar hijri": "jalali",
    "persian calendar": "jalali",
    "gregorian": "gregorian",
    "میلادی": "gregorian",
}
JALALI_MONTHS = {
    "فروردین": 1,
    "اردیبهشت": 2,
    "خرداد": 3,
    "تیر": 4,
    "مرداد": 5,
    "امرداد": 5,
    "شهریور": 6,
    "مهر": 7,
    "ابان": 8,
    "اذر": 9,
    "دی": 10,
    "بهمن": 11,
    "اسفند": 12,
    "farvardin": 1,
    "ordibehesht": 2,
    "khordad": 3,
    "tir": 4,
    "mordad": 5,
    "shahrivar": 6,
    "mehr": 7,
    "aban": 8,
    "azar": 9,
    "dey": 10,
    "bahman": 11,
    "esfand": 12,
}
GREGORIAN_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
    "ژانویه": 1,
    "فوریه": 2,
    "مارس": 3,
    "اوریل": 4,
    "مه": 5,
    "ژوئن": 6,
    "ژوئیه": 7,
    "اوت": 8,
    "سپتامبر": 9,
    "اکتبر": 10,
    "نوامبر": 11,
    "دسامبر": 12,
}
# Month names that are also ordinary words need a year or a preposition next to them.
AMBIGUOUS_MONTHS = {"may", "mar", "jan", "jun", "jul", "aug", "sep", "dec", "مه", "tir", "dey"}
MONTH_PREPOSITIONS = {"in", "during", "for", "of", "در", "طی", "ماه"}
SINCE_WORDS = {"since", "from", "after", "از"}
UNTIL_WORDS = {"until", "till", "before", "to", "تا"}
STOPWORDS = {
    *"the a an of in on at for by per and or with to from show me give get list what whats is"
    " was are were how much many total sum all each every our we i do did has have had which"
    " that this these those my please report tell about value values".split(),
    *"در از به با و را برای که این ان هر تا ها های بر اساس تفکیک چقدر چند کل مجموع نشان بده"
    " بدهید بدید لطفا گزارش میزان مقدار است بود شد داشته داشتیم داشتند چه کدام همه چی رو"
    " می خواهم میخواهم اطلاعات وضعیت شده".split(),
}

# Words that carry no meaning for a report (never reported as "not used").
FILLER = {
    *"now only instead also add make it just show give then too as well chart graph number"
    " please can you could would like want see over during since vs versus".split(),
    *"حالا فقط به جای هم اضافه کن کنید رو صورت اش بر حسب نظر نمودار چطور چی ببینم"
    " بگو بده لطفا میشه می شه".split(),
}

_DATE_TOKEN = re.compile(r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}$")
_NUMBER_TOKEN = re.compile(r"^\d+(?:[.,]\d+)*$")


def _lex(phrases) -> list[tuple[str, Any]]:
    if isinstance(phrases, dict):
        return [(p, v) for p, v in phrases.items()]
    return [(p, p) for p in phrases]


def _number(word: str) -> float | None:
    if word in NUMBER_WORDS:
        return NUMBER_WORDS[word]
    if _NUMBER_TOKEN.match(word):
        clean = word.replace(",", "")
        try:
            return float(clean)
        except ValueError:
            return None
    return None


def _int(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


# --- parse state ---------------------------------------------------------------------


@dataclass
class Parsed:
    words: list[str]
    taken: set[int] = field(default_factory=set)
    period: str | None = None
    all_time: bool = False
    date_filter: tuple[str, Any] | None = None  # (op, value) on the time dimension
    grain: str | None = None
    chart: str | None = None
    calendar: str | None = None
    top: tuple[str, int | None] | None = None  # (direction, N)
    top_at: int | None = None
    which: bool = False
    order: str | None = None
    implied_measure: str | None = None
    comparisons: list[tuple[int, str, Any]] = field(default_factory=list)  # (pos, op, value)
    compare: tuple[str, str | None] | None = None  # (compare_to, unit) — see COMPARE_PHRASES
    no_compare: bool = False

    def free(self, i: int) -> bool:
        return 0 <= i < len(self.words) and i not in self.taken

    def claim(self, hits: list[Hit]) -> None:
        for h in hits:
            self.taken |= set(range(h.start, h.end))


def _find(p: Parsed, phrases) -> list[Hit]:
    return find_phrases(p.words, _lex(phrases), p.taken)


def _has(words: list[str], phrases) -> bool:
    return bool(find_phrases(words, _lex(phrases)))


def parse_compare(p: Parsed) -> None:
    """ "compared to last year", «نسبت به ماه قبل», "yoy"… — claimed before the time
    parser, so the period compared against isn't read as the report's own period."""
    hits = _find(p, NO_COMPARE)
    if hits:
        p.no_compare = True
        p.claim(hits)
    hits = _find(p, COMPARE_PHRASES)
    if hits:
        p.compare = hits[0].payload
        p.claim(hits)


def parse_time(p: Parsed, today, calendar: str) -> None:
    words = p.words
    # explicit dates: two → between, one → since/until/that day
    found = [i for i, w in enumerate(words) if _DATE_TOKEN.match(w)]
    if len(found) >= 2:
        a, b = found[:2]
        p.date_filter = ("between", [words[a], words[b]])
        p.taken |= {a, b}
    elif len(found) == 1:
        i = found[0]
        before = words[i - 1] if i > 0 else ""
        if before in SINCE_WORDS:
            p.date_filter = ("gte", words[i])
            p.taken |= {i - 1, i}
        elif before in UNTIL_WORDS:
            p.date_filter = ("lte", words[i])
            p.taken |= {i - 1, i}
        else:
            p.date_filter = ("between", [words[i], words[i]])
            p.taken.add(i)
    # month names (with or without a year) and bare years
    if p.date_filter is None:
        for i, w in enumerate(words):
            if not p.free(i):
                continue
            month, cal = None, None
            if w in JALALI_MONTHS:
                month, cal = JALALI_MONTHS[w], "jalali"
            elif w in GREGORIAN_MONTHS:
                month, cal = GREGORIAN_MONTHS[w], "gregorian"
            if month is None:
                continue
            nxt = words[i + 1] if i + 1 < len(words) else ""
            prev = words[i - 1] if i > 0 else ""
            year = int(nxt) if nxt.isdigit() and len(nxt) == 4 else None
            if year is None and w in AMBIGUOUS_MONTHS and prev not in MONTH_PREPOSITIONS:
                continue
            if year is not None:
                cal = "jalali" if year < 1700 else "gregorian"
                sep = "/" if cal == "jalali" else "-"
                value = f"{year}{sep}{month:02d}"
                p.taken |= {i, i + 1}
            else:
                value = dates.most_recent_month(month, today, cal)
                p.taken.add(i)
            if prev == "ماه":
                p.taken.add(i - 1)
            bounds = dates.partial_bounds(value)
            p.date_filter = ("between", [bounds[0].isoformat(), bounds[1].isoformat()])
            break
    if p.date_filter is None:
        for i, w in enumerate(words):
            if (
                p.free(i)
                and w.isdigit()
                and len(w) == 4
                and (1300 <= int(w) < 1500 or 1900 <= int(w) <= 2100)
            ):
                bounds = dates.partial_bounds(w)
                p.date_filter = ("between", [bounds[0].isoformat(), bounds[1].isoformat()])
                p.taken.add(i)
                if i > 0 and words[i - 1] in ("سال", "year", "in", "during", "در"):
                    p.taken.add(i - 1)
                break
    if p.date_filter is not None and p.date_filter[0] == "between":
        lo, hi = p.date_filter[1]
        p.date_filter = (
            "between",
            [dates.expand_filter("gte", lo)[1], dates.expand_filter("lte", hi)[1]],
        )
    elif p.date_filter is not None:
        p.date_filter = dates.expand_filter(*p.date_filter)

    hits = _find(p, ALL_TIME)
    if hits:
        p.all_time = True
        p.claim(hits)
    hits = _find(p, FIXED_PERIODS)
    if hits:
        p.period = hits[0].payload
        p.claim(hits)
    if p.period is None:
        _relative(p)
    hits = _find(p, CALENDAR_WORDS)
    if hits:
        p.calendar = hits[0].payload
        p.claim(hits)


def _relative(p: Parsed) -> None:
    words = p.words
    for i, w in enumerate(words):
        if not p.free(i):
            continue
        # English: (this|last|past) [N] unit
        if w in THIS_WORDS | LAST_WORDS and w != "این":
            j = i + 1
            if j < len(words) and words[j] == "the":
                j += 1
            n = _number(words[j]) if j < len(words) else None
            if n is not None and j + 1 < len(words) and words[j + 1] in UNITS:
                unit = UNITS[words[j + 1]]
                kind = "past" if w == "past" else "last"
                if w in THIS_WORDS:
                    continue
                p.period = f"{kind}_{_int(n)}_{unit}s"
                p.taken |= set(range(i, j + 2))
                if i > 0 and words[i - 1] in ("the", "in", "over", "for", "during"):
                    p.taken.add(i - 1)
                return
            if j < len(words) and words[j] in UNITS:
                unit = UNITS[words[j]]
                p.period = f"{'this' if w in THIS_WORDS else 'last'}_{unit}"
                p.taken |= set(range(i, j + 1))
                return
        # Persian: [N] unit (گذشته|اخیر|…) / unit (جاری|…) / این unit
        if w == "این" and i + 1 < len(words) and words[i + 1] in UNITS:
            p.period = f"this_{UNITS[words[i + 1]]}"
            p.taken |= {i, i + 1}
            return
        if w in UNITS and i + 1 < len(words):
            unit = UNITS[w]
            after = words[i + 1]
            n = _number(words[i - 1]) if i > 0 and p.free(i - 1) else None
            if after in FA_LAST_AFTER:
                if n is not None:
                    p.period = f"last_{_int(n)}_{unit}s"
                    p.taken |= {i - 1, i, i + 1}
                else:
                    p.period = f"last_{unit}"
                    p.taken |= {i, i + 1}
                return
            if after in FA_THIS_AFTER:
                p.period = f"this_{unit}"
                p.taken |= {i, i + 1}
                return
        # "N unit ago/back" in English: "3 months back" ~ last_3_months
        if w in UNITS and i + 1 < len(words) and words[i + 1] in ("ago", "back"):
            n = _number(words[i - 1]) if i > 0 else None
            if n is not None:
                p.period = f"last_{_int(n)}_{UNITS[w]}s"
                p.taken |= {i - 1, i, i + 1}
                return


def _read_number(p: Parsed, i: int) -> tuple[float, int] | None:
    """A number starting at word ``i`` (with a multiplier word): (value, words used)."""
    if not p.free(i):
        return None
    n = _number(p.words[i])
    if n is None or p.words[i] in ("a", "an"):
        return None
    used = 1
    if i + 1 < len(p.words) and p.words[i + 1] in MULTIPLIERS:
        n *= MULTIPLIERS[p.words[i + 1]]
        used = 2
    return n, used


def parse_comparisons(p: Parsed) -> None:
    words = p.words
    # between N and M / بین N و M ("between 1 and 5 million": the multiplier is shared)
    for i, w in enumerate(words):
        if w in ("between", "بین") and p.free(i):
            a = _read_number(p, i + 1)
            if a and i + 1 + a[1] < len(words) and words[i + 1 + a[1]] in ("and", "و", "تا"):
                b = _read_number(p, i + 2 + a[1])
                if b:
                    low = a[0]
                    if a[1] == 1 and b[1] == 2:
                        low *= MULTIPLIERS[words[i + 3 + a[1]]]
                    p.comparisons.append((i, "between", [_int(low), _int(b[0])]))
                    p.taken |= set(range(i, i + 2 + a[1] + b[1]))
    # "more than N" / "کمتر از N": one phrase list, so the longest phrase wins
    phrases = [(ph, "gte") for ph in GTE_WORDS] + [(ph, "lte") for ph in LTE_WORDS]
    for hit in find_phrases(words, phrases, set(p.taken)):
        num = _read_number(p, hit.end)
        # bare "از"/"تا" (from/to) only count before an amount, never before a year
        if num is None or (hit.phrase in ("از", "تا") and num[0] < 100):
            continue
        if words[hit.end].isdigit() and len(words[hit.end]) == 4 and num[1] == 1:
            continue
        p.comparisons.append((hit.start, hit.payload, _int(num[0])))
        p.taken |= set(range(hit.start, hit.end + num[1]))
    # Persian puts the comparative after the number: "۵ میلیون به بالا"
    for i, w in enumerate(words):
        if w in ("بالا", "بیشتر") and i >= 2 and words[i - 1] == "به":
            for start in (i - 3, i - 2):
                num = _read_number(p, start) if start >= 0 else None
                if num and start + num[1] == i - 1:
                    p.comparisons.append((start, "gte", _int(num[0])))
                    p.taken |= set(range(start, i + 1))
                    break


def parse_top(p: Parsed) -> None:
    words = p.words
    for phrases, direction in ((TOP_DESC, "desc"), (TOP_ASC, "asc")):
        hits = find_phrases(words, _lex(phrases), set(p.taken))
        if not hits:
            continue
        hit = hits[0]
        if hit.phrase in ("اول", "اخر"):
            # only "۵ تا اول" / "۵ مورد اول" / "N <noun> اول"
            if not any(_number(words[j]) for j in range(max(0, hit.start - 3), hit.start)):
                continue
        n = None
        for j in [
            *range(hit.end, min(len(words), hit.end + 2)),
            *range(hit.start - 1, max(-1, hit.start - 4), -1),
        ]:
            if p.free(j) and j not in range(hit.start, hit.end):
                value = _number(words[j])
                if value is not None and words[j] not in ("a", "an", "نه") and value < 1000:
                    n = int(value)
                    p.taken.add(j)
                    if j + 1 < len(words) and words[j + 1] in ("تا", "مورد", "تای"):
                        p.taken.add(j + 1)
                    break
        p.top = (direction, n)
        p.top_at = hit.start
        p.taken |= set(range(hit.start, hit.end))
        if hit.phrase in IMPLIED_MEASURE:
            p.implied_measure = IMPLIED_MEASURE[hit.phrase]
        if hit.phrase == "most" and hit.start > 0 and words[hit.start - 1] == "the":
            p.taken.add(hit.start - 1)
        return


def parse_misc(p: Parsed) -> None:
    hits = _find(p, GRAINS)
    if hits:
        p.grain = hits[0].payload
        p.claim(hits)
    hits = _find(p, CHARTS)
    if hits:
        p.chart = hits[0].payload
        p.claim(hits)
        for i in range(len(p.words)):  # "as a pie chart", "نمودار ..."
            if p.words[i] in ("chart", "graph", "نمودار") and p.free(i):
                p.taken.add(i)
    hits = _find(p, ORDER_WORDS)
    if hits:
        p.order = hits[0].payload
        p.claim(hits)
    if p.grain is None:
        hits = _find(p, TREND_WORDS)
        if hits:
            p.grain = "month"
            p.claim(hits)
    p.which = any(w in WHICH_WORDS for w in p.words)


# --- catalog matching ----------------------------------------------------------------

KIND_ORDER = {"measure": 0, "dimension": 1, "choice": 2, "model": 3}


@dataclass(frozen=True)
class Ref:
    kind: str
    model: str
    key: str = ""
    value: Any = None


def catalog_phrases(catalog: list[ModelEntry]) -> dict[str, list[Ref]]:
    out: dict[str, list[Ref]] = {}

    def add(phrase: str, ref: Ref):
        key = normalise(phrase)
        if key and ref not in out.setdefault(key, []):
            out[key].append(ref)

    for entry in catalog:
        for m in entry.measures.values():
            for ph in m.phrases():
                add(ph, Ref("measure", entry.name, m.key))
        for d in entry.dimensions.values():
            for ph in d.phrases():
                add(ph, Ref("dimension", entry.name, d.key))
            for value, labels in d.choices or []:
                for ph in {str(value), *labels.values()}:
                    add(ph, Ref("choice", entry.name, d.key, value))
        for ph in entry.phrases():
            add(ph, Ref("model", entry.name))
    for refs in out.values():
        refs.sort(key=lambda r: KIND_ORDER[r.kind])
    return out


@dataclass
class Mention:
    start: int
    end: int
    refs: list[Ref]


def _negated(words: list[str], start: int) -> bool:
    window = words[max(0, start - 3) : start]
    return bool(find_phrases(window, _lex(NEGATIONS)))


def _grouping_cue(words: list[str], start: int) -> bool:
    return any(w in GROUP_CUES for w in words[max(0, start - 2) : start])


# --- the provider --------------------------------------------------------------------


class HeuristicProvider(Provider):
    """Rule-based English/Persian interpreter; no network, no model."""

    name = "heuristic"
    local = True
    model = "rules-v1"

    def generate(self, task: Task) -> Generation:
        interpreter = Interpreter(task)
        spec = interpreter.run()
        notes = []
        ignored = interpreter.ignored()
        if ignored and not spec.get("clarification_needed"):
            notes.append(_("Not used: %(words)s") % {"words": _(", ").join(ignored)})
        return Generation(spec, json.dumps(spec, ensure_ascii=False), [], notes)


class Interpreter:
    def __init__(self, task: Task):
        self.task = task
        self.p = Parsed(tokens(task.question))
        cased = tokens(task.question, keep_case=True)  # for echoing the user's words
        self.cased = cased if len(cased) == len(self.p.words) else self.p.words
        self.catalog = {e.name: e for e in task.catalog}

    # -- helpers ------------------------------------------------------------------------

    def ignored(self) -> list[str]:
        """Words of the question nothing used (shown, so a misread is visible)."""
        p = self.p
        return [
            self.cased[i]
            for i, w in enumerate(p.words)
            if p.free(i)
            and w not in STOPWORDS
            and w not in FILLER
            and _number(w) is None
            and len(w) > 1
        ]

    def clarify(self, message: str, model: str | None = None) -> dict[str, Any]:
        entry = self.catalog.get(model) if model else next(iter(self.catalog.values()), None)
        return {
            "semantic_model": entry.name if entry else "",
            "measures": [next(iter(entry.measures))] if entry and entry.measures else [],
            "clarification_needed": message,
        }

    def mentions(self) -> list[Mention]:
        """Catalog phrases in the question; a span keeps every reference whose
        phrase covers it exactly ("customers": the measure and the dimension)."""
        phrases = catalog_phrases(self.task.catalog)
        pairs = [(ph, ph) for ph in phrases]
        hits = find_phrases(self.p.words, pairs, self.p.taken)
        out = []
        for h in hits:
            span = self.p.words[h.start : h.end]
            refs = [
                r
                for ph, _ph in pairs
                for r in phrases[ph]
                if any(x.size == len(span) for x in find_phrases(span, [(ph, None)]))
            ]
            refs = sorted(dict.fromkeys(refs), key=lambda r: KIND_ORDER[r.kind])
            out.append(Mention(h.start, h.end, refs))
        return out

    def pick_model(self, mentions: list[Mention]) -> tuple[str | None, str | None]:
        """(model name, clarification) — the model the question is about."""
        previous = self.task.previous
        scores: dict[str, float] = {}
        named: set[str] = set()
        for m in mentions:
            for model in {r.model for r in m.refs}:
                kinds = {r.kind for r in m.refs if r.model == model}
                scores[model] = scores.get(model, 0) + (2 if kinds == {"model"} else 1)
                if "model" in kinds:
                    named.add(model)
        if previous and previous.get("semantic_model") in self.catalog:
            prev = previous["semantic_model"]
            others = named - {prev}
            if not others:
                return prev, None
            if len(others) == 1:
                return others.pop(), None
        if not scores:
            if len(self.catalog) == 1:
                return next(iter(self.catalog)), None
            return None, _("Which data do you mean: %(options)s?") % {
                "options": self._or([self._label(e) for e in self.catalog.values()])
            }
        best = max(scores.values())
        top = [m for m, s in scores.items() if s == best]
        if len(top) > 1:
            labels = [self._label(self.catalog[m]) for m in top]
            return None, _("Which data do you mean: %(options)s?") % {"options": self._or(labels)}
        return top[0], None

    def _label(self, item) -> str:
        return item.label.get(self.task.language) or item.label.get("en") or ""

    def _or(self, labels: list[str]) -> str:
        labels = [x for x in labels if x]
        if len(labels) <= 1:
            return "".join(labels)
        comma = "، " if self.task.language == "fa" else ", "
        return _("%(items)s or %(last)s") % {"items": comma.join(labels[:-1]), "last": labels[-1]}

    # -- main ---------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        task, p = self.task, self.p
        if not self.catalog:
            return {
                "semantic_model": "",
                "measures": [],
                "clarification_needed": _("There is no data you may report on."),
            }
        parse_compare(p)
        parse_time(p, task.today, task.calendar)
        parse_comparisons(p)
        parse_top(p)
        parse_misc(p)
        mentions = self._mentions = self.mentions()
        model, question = self.pick_model(mentions)
        if model is None:
            return self.clarify(question or "")
        entry = self.catalog[model]
        words = p.words

        measures: list[str] = []
        groups: list[str] = []
        time_dims: list[str] = []
        filters: dict[str, dict[str, Any]] = {}
        filter_dims: set[str] = set()
        ambiguous: list[list[str]] = []
        all_dims: set[str] = set()
        removed: dict[str, list[str]] = {"measure": [], "dimension": []}
        remove_words = set(REMOVE_CUES)

        def is_removal(start: int) -> bool:
            return any(w in remove_words for w in words[max(0, start - 2) : start])

        last_measure_end = -10
        for m in mentions:
            refs = [r for r in m.refs if r.model == model]
            if not refs:
                continue
            kinds = {r.kind for r in refs}
            qualifier = "measure" in kinds and "dimension" not in kinds and bool(measures)
            if qualifier and self._qualifies(last_measure_end, m.start):
                continue  # "revenue from orders over 2M": the second is a noun, not a measure
            if "measure" in kinds and "dimension" in kinds:
                # "top customers" / "by customer": a dimension; otherwise a measure
                near_top = p.top_at is not None and 0 <= m.start - p.top_at <= 3
                if near_top or _grouping_cue(words, m.start):
                    refs = [r for r in refs if r.kind == "dimension"]
                else:
                    refs = [r for r in refs if r.kind == "measure"]
                kinds = {r.kind for r in refs}
            kind = refs[0].kind
            same = [r for r in refs if r.kind == kind]
            if kind == "measure":
                keys = [r.key for r in same]
                if len(keys) > 1:
                    ambiguous.append(keys)
                    continue
                if is_removal(m.start):
                    removed["measure"].append(keys[0])
                elif keys[0] not in measures:
                    measures.append(keys[0])
                last_measure_end = m.end
            elif kind == "dimension":
                keys = [r.key for r in same]
                if len(keys) > 1:
                    ambiguous.append(keys)
                    continue
                key = keys[0]
                dim = entry.dimensions[key]
                if dim.type == "time":
                    time_dims.append(key)
                elif is_removal(m.start):
                    removed["dimension"].append(key)
                elif m.start > 0 and words[m.start - 1] in ALL_CUES:
                    all_dims.add(key)
                elif key not in groups:
                    groups.append(key)
            elif kind == "choice":
                ref = same[0]
                self._add_filter(filters, ref.key, ref.value, _negated(words, m.start))
                filter_dims.add(ref.key)
            # a bare model mention needs nothing

        if ambiguous:
            labels = [
                self._label(entry.measures.get(k) or entry.dimensions[k]) for k in ambiguous[0]
            ]
            return self.clarify(
                _("Did you mean %(options)s?") % {"options": self._or(labels)}, model
            )

        # category values ("Tehran", "تهران") through the scoped value index
        value_spans: list[tuple[int, int]] = []
        matcher = task.values(model) if task.values else None
        if matcher is not None:
            for key, value, start, end in self._value_mentions(matcher, mentions, model):
                self._add_filter(filters, key, value, _negated(words, start))
                filter_dims.add(key)
                value_spans.append((start, end))
            # "شهر نیویورک": an unknown word right after a category dimension's name is
            # a value the user expects to exist; value matching will say it doesn't
            for key, text, _start in self._unknown_values(mentions, model, filter_dims):
                filters[key] = {"field": key, "op": "eq", "value": text}
                filter_dims.add(key)
        # a dimension named right next to its own filter value is the filter's label;
        # one right before another dimension's value qualifies it ("مشتریان اصفهان")
        groups = [
            g
            for g in groups
            if not self._labels_filter(g, mentions, filter_dims, model)
            and not self._qualifier(g, mentions, value_spans, model)
        ]

        # number comparisons → the nearest mentioned number dimension, else the only one
        numbers = [k for k, d in entry.dimensions.items() if d.type == "number"]
        for pos, op, value in p.comparisons:
            target = None
            near = [
                (abs(m.start - pos), r.key)
                for m in mentions
                for r in m.refs
                if r.model == model and r.kind == "dimension" and r.key in numbers
            ]
            if near:
                target = min(near)[1]
            elif len(numbers) == 1:
                target = numbers[0]
            if target is not None:
                filters[f"{target}:{op}"] = {"field": target, "op": op, "value": value}
                groups = [g for g in groups if g != target]

        if not measures and p.implied_measure:
            measures = self._implied(entry, p.implied_measure)

        previous = (
            task.previous
            if task.previous and task.previous.get("semantic_model") == model
            else None
        )
        if previous:
            return self._refine(
                entry, previous, measures, groups, time_dims, filters, removed, all_dims
            )
        if not measures:
            labels = [self._label(m) for m in entry.measures.values()][:6]
            return self.clarify(
                _("What should I measure? For example: %(options)s.")
                % {"options": self._or(labels)},
                model,
            )
        spec: dict[str, Any] = {"semantic_model": model, "measures": measures}
        self._apply_time(spec, entry, time_dims)
        if groups:
            spec["dimensions"] = groups
        flist = list(filters.values())
        if p.date_filter is not None:
            tdim = spec.get("time_dimension") or entry.time_dimension
            if tdim:
                flist.append({"field": tdim, "op": p.date_filter[0], "value": p.date_filter[1]})
        if flist:
            spec["filters"] = flist
        if p.period and not p.all_time:
            spec["relative_period"] = p.period
        if p.calendar:
            spec["calendar"] = p.calendar
        problem = self._apply_compare(spec) or self._apply_sort(spec, entry)
        if problem:
            return self.clarify(problem, model)
        spec["chart"] = p.chart or infer_chart(spec)
        return spec

    # -- pieces ---------------------------------------------------------------------------

    def _add_filter(self, filters, key, value, negated) -> None:
        current = filters.get(key)
        op = "neq" if negated else "eq"
        if current is None:
            filters[key] = {"field": key, "op": op, "value": value}
            return
        values = current["value"] if isinstance(current["value"], list) else [current["value"]]
        if value not in values:
            values.append(value)
        neg = current["op"] in ("neq", "not_in") or negated
        current["op"] = ("not_in" if neg else "in") if len(values) > 1 else op
        current["value"] = values if len(values) > 1 else values[0]

    def _implied(self, entry: ModelEntry, phrase: str) -> list[str]:
        target = normalise(phrase)
        for m in entry.measures.values():
            if any(normalise(x) == target for x in m.phrases()):
                return [m.key]
        return []

    def _labels_filter(
        self, key: str, mentions: list[Mention], filter_dims: set[str], model
    ) -> bool:
        return key in filter_dims and not any(
            _grouping_cue(self.p.words, m.start)
            for m in mentions
            if any(r.kind == "dimension" and r.key == key and r.model == model for r in m.refs)
        )

    def _value_mentions(self, matcher, mentions: list[Mention], model: str):
        """(dimension, value, position) for every category value named in the question."""
        p = self.p
        words = p.words
        dim_at = {
            i: r.key
            for m in mentions
            for r in m.refs
            if r.model == model and r.kind == "dimension"
            for i in range(m.start, m.end)
        }
        out = []
        i = 0
        while i < len(words):
            if not p.free(i) or words[i] in STOPWORDS or _number(words[i]) is not None:
                i += 1
                continue
            for size in (3, 2, 1):
                span = range(i, i + size)
                if span.stop > len(words) or not all(p.free(j) for j in span):
                    continue
                if words[span.stop - 1] in STOPWORDS:
                    continue
                text = " ".join(words[j] for j in span)
                found = matcher.mentions(text, fuzzy=size == 1 and len(text) >= 5)
                if not found:
                    continue
                # a dimension named right before/after the value decides; else the
                # best match kind, then declaration order
                near = [dim_at.get(j) for j in (i - 1, i - 2, span.stop, span.stop + 1)]
                key = next((k for k in near if k in found), None)
                if key is None:
                    rank = {"exact": 0, "phonetic": 1, "fuzzy": 2}
                    key = min(
                        found,
                        key=lambda k: (rank[found[k][0].how], list(matcher.dimensions).index(k)),
                    )
                out.append((key, found[key][0].value, i, span.stop))
                p.taken |= set(span)
                i = span.stop - 1
                break
            i += 1
        return out

    def _qualifier(self, key, mentions: list[Mention], value_spans, model) -> bool:
        for m in mentions:
            if not any(r.kind == "dimension" and r.key == key and r.model == model for r in m.refs):
                continue
            if _grouping_cue(self.p.words, m.start):
                return False
            if any(start == m.end for start, _end in value_spans):
                return True
        return False

    def _qualifies(self, previous_end: int, start: int) -> bool:
        """A measure word right after another measure ("درآمد سفارش‌های…", "revenue
        from orders") qualifies it instead of adding a second measure."""
        between = self.p.words[previous_end:start]
        return previous_end >= 0 and len(between) <= 1 and set(between) <= {"from", "of", "on"}

    def _unknown_values(self, mentions: list[Mention], model: str, filter_dims: set[str]):
        p, words = self.p, self.p.words
        entry = self.catalog[model]
        for m in mentions:
            refs = [r for r in m.refs if r.model == model and r.kind == "dimension"]
            if len(refs) != 1 or refs[0].key in filter_dims or _grouping_cue(words, m.start):
                continue
            dim = entry.dimensions[refs[0].key]
            if dim.type not in ("category", "geo") or dim.choices:
                continue
            span = []
            for j in range(m.end, min(len(words), m.end + 2)):
                if not p.free(j) or words[j] in STOPWORDS or _number(words[j]) is not None:
                    break
                span.append(j)
            if span:
                p.taken |= set(span)
                yield dim.key, " ".join(self.cased[j] for j in span), m.start

    def _apply_time(self, spec, entry: ModelEntry, time_dims: list[str]) -> None:
        p = self.p
        tdim = time_dims[0] if time_dims else None
        if tdim and tdim != entry.time_dimension:
            spec["time_dimension"] = tdim
        if p.grain:
            spec["time_grain"] = p.grain
        elif tdim and _grouping_cue(p.words, self._position(tdim)):
            spec["time_grain"] = "day"

    def _position(self, key: str) -> int:
        for m in self._mentions:
            if any(r.key == key for r in m.refs):
                return m.start
        return 0

    def _apply_sort(self, spec, entry: ModelEntry) -> str | None:
        p = self.p
        if p.top is None and p.order is None:
            return None
        if p.top is not None:
            direction, n = p.top
            if not spec.get("dimensions") and not spec.get("time_grain"):
                labels = [self._label(d) for d in entry.dimensions.values() if d.type != "time"][:6]
                return _("Top of what? For example: %(options)s.") % {"options": self._or(labels)}
            spec["sort"] = [{"field": spec["measures"][0], "direction": direction}]
            spec["limit"] = n if n is not None else (1 if p.which else 10)
        else:
            spec["sort"] = [{"field": spec["measures"][0], "direction": p.order}]
        return None

    def _apply_compare(self, spec: dict[str, Any]) -> str | None:
        """Set ``compare_to``; a comparison without a period compares the current
        unit to date ("revenue vs last month" → this month vs the previous one)."""
        if self.p.compare is None:
            return None
        kind, unit = self.p.compare
        spec["compare_to"] = kind
        dated = spec.get("relative_period") or any(
            f.get("op") in ("between", "eq") and f.get("field") == self.time_dimension(spec)
            for f in spec.get("filters") or []
        )
        if dated:
            return None
        if unit is None and kind == _SPLY:
            unit = "year"
        if unit is None:
            return _("Which period should I compare? For example: “this month compared to the "
                     "previous period”.")  # fmt: skip
        spec["relative_period"] = f"this_{unit}"
        return None

    def time_dimension(self, spec: dict[str, Any]) -> str | None:
        entry = self.catalog.get(spec.get("semantic_model", ""))
        return spec.get("time_dimension") or (entry.time_dimension if entry else None)

    def _refine(self, entry, previous, measures, groups, time_dims, filters, removed, all_dims):
        p = self.p
        words = p.words
        adding = _has(words, ADD_CUES)
        spec = {k: v for k, v in previous.items() if v not in (None, [], "")}
        spec.pop("clarification_needed", None)
        old_chart = previous.get("chart") or "table"
        was_default = old_chart == infer_chart(previous)
        if measures:
            spec["measures"] = (
                list(dict.fromkeys([*previous.get("measures", []), *measures]))
                if adding
                else measures
            )
        if removed["measure"]:
            spec["measures"] = [
                m for m in spec.get("measures", []) if m not in removed["measure"]
            ] or spec["measures"]
        dims = list(previous.get("dimensions") or [])
        if groups:
            dims = list(dict.fromkeys([*dims, *groups])) if adding else groups
        dims = [d for d in dims if d not in removed["dimension"]]
        spec["dimensions"] = dims
        flist = [dict(f) for f in previous.get("filters") or []]
        tdim = time_dims[0] if time_dims else previous.get("time_dimension") or entry.time_dimension
        new_fields = {f["field"] for f in filters.values()} | set(removed["dimension"]) | all_dims
        if p.date_filter is not None or p.period or p.all_time:
            new_fields.add(tdim)
            spec.pop("relative_period", None)
        flist = [f for f in flist if f.get("field") not in new_fields]
        flist += list(filters.values())
        if p.date_filter is not None and tdim:
            flist.append({"field": tdim, "op": p.date_filter[0], "value": p.date_filter[1]})
        spec["filters"] = flist
        if p.period and not p.all_time:
            spec["relative_period"] = p.period
        if time_dims and time_dims[0] != entry.time_dimension:
            spec["time_dimension"] = time_dims[0]
        if p.grain:
            spec["time_grain"] = p.grain
        if p.calendar:
            spec["calendar"] = p.calendar
        if p.no_compare:
            spec.pop("compare_to", None)
        problem = self._apply_compare(spec)
        if problem:
            return self.clarify(problem, entry.name)
        if p.top is not None or p.order is not None:
            problem = self._apply_sort(spec, entry)
            if problem:
                return self.clarify(problem, entry.name)
        # keep only sort keys still in the report
        selected = set(spec.get("measures", [])) | set(spec.get("dimensions", []))
        if spec.get("time_grain"):
            selected.add(spec.get("time_dimension") or entry.time_dimension)
        spec["sort"] = [s for s in spec.get("sort") or [] if s.get("field") in selected]
        for key in ("dimensions", "filters", "sort"):
            if not spec.get(key):
                spec.pop(key, None)
        if p.chart:
            spec["chart"] = p.chart
        elif was_default:
            spec["chart"] = infer_chart(spec)
        return spec


def infer_chart(spec: dict[str, Any]) -> str:
    """The chart the NL layer picks when the question names none."""
    dims = spec.get("dimensions") or []
    if spec.get("time_grain"):
        return "line" if len(dims) <= 1 else "table"
    if not dims:
        return "kpi"
    return "bar" if len(dims) == 1 else "table"


# token_matches is re-exported for the evaluation tools
__all__ = ["HeuristicProvider", "Interpreter", "infer_chart", "token_matches"]
