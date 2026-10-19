"""Text normalisation, tokenisation and Persian ⇄ Latin transliteration keys.

Everything the NL layer compares — catalog labels, user questions, category
values from the database — goes through :func:`normalise` first, so Arabic
``ي``/``ك``, Persian/Arabic digits, the zero-width non-joiner, diacritics and
case never decide a match.

:func:`phonetic_keys` maps a name in either script to a consonant skeleton
(``تهران`` and ``Tehran`` both become ``thrn``), which is how ``تهران`` finds
``Tehran`` in an English database and vice versa. It is deliberately lossy:
short vowels are not written in Persian, so vowels are dropped on both sides.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from functools import lru_cache

_LETTERS = str.maketrans(
    {
        "ي": "ی",
        "ى": "ی",
        "ئ": "ی",
        "ك": "ک",
        "ة": "ه",
        "ۀ": "ه",
        "أ": "ا",
        "إ": "ا",
        "ٱ": "ا",
        "ؤ": "و",
        "‌": " ",  # ZWNJ: "سفارش‌ها" → "سفارش ها"
        "‍": "",
        "ـ": "",  # tatweel
        "،": ",",
        "؛": ";",
        "؟": "?",
        "٫": ".",
        "٬": ",",
    }
)
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
TO_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_TOKEN_RE = re.compile(r"[\w]+(?:[.,/\-][\w]+)*", re.UNICODE)
_ARABIC_SCRIPT = re.compile(r"[؀-ۿ]")


def normalise(value: object, lower: bool = True) -> str:
    """Lower-case, Persian letters, Latin digits, no diacritics, single spaces."""
    s = str(value).translate(_LETTERS).translate(DIGITS)
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = s.replace("آ", "ا")  # NFKD splits آ into ا + madda; keep the base letter only
    return " ".join((s.lower() if lower else s).split())


def tokens(value: object, keep_case: bool = False) -> list[str]:
    """Words and numbers (``1405/01/01`` and ``1,000`` stay one token)."""
    return _TOKEN_RE.findall(normalise(value, lower=not keep_case))


def is_persian(value: str) -> bool:
    return bool(_ARABIC_SCRIPT.search(value))


def persian_digits(value: object) -> str:
    return str(value).translate(TO_PERSIAN_DIGITS)


# --- transliteration ----------------------------------------------------------------

# Persian letters → a Latin consonant class; "" drops the letter (a vowel carrier).
_FA = {
    "ا": "",
    "آ": "",
    "ع": "",
    "ء": "",
    "ب": "b",
    "پ": "p",
    "ت": "t",
    "ط": "t",
    "ث": "s",
    "س": "s",
    "ص": "s",
    "ج": "j",
    "چ": "c",
    "ح": "h",
    "ه": "h",
    "خ": "x",
    "د": "d",
    "ذ": "z",
    "ز": "z",
    "ض": "z",
    "ظ": "z",
    "ر": "r",
    "ژ": "Z",
    "ش": "S",
    "غ": "q",
    "ق": "q",
    "ف": "f",
    "ک": "k",
    "گ": "g",
    "ل": "l",
    "م": "m",
    "ن": "n",
}
# Latin digraphs first, then single letters; vowels drop.
_LATIN_DIGRAPHS = [("kh", "x"), ("sh", "S"), ("ch", "c"), ("zh", "Z"), ("gh", "q"), ("ph", "f")]
_LATIN = {
    "c": "k",
    "q": "q",
    "w": "v",
    "x": "x",
    "y": "y",
    "a": "",
    "e": "",
    "i": "",
    "o": "",
    "u": "",
}


def _collapse(key: str) -> str:
    out = []
    for ch in key:
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)


def _latin_key(word: str) -> str:
    for src, dst in _LATIN_DIGRAPHS:
        word = word.replace(src, dst)
    out = []
    for ch in word:
        if ch in _LATIN:
            out.append(_LATIN[ch])
        elif ch.isascii() and (ch.isalpha() or ch in "SZ"):
            out.append(ch)
        elif ch.isdigit():
            out.append(ch)
    return _collapse("".join(out))


def _persian_keys(word: str) -> set[str]:
    """``و``/``ی`` are consonants (v/y) or vowels (u/o, i/e): every reading (capped),
    with a word-initial one always a consonant (``یزد`` → ``yzd``)."""
    readings = [""]
    for i, ch in enumerate(word):
        if ch in "وی":
            c = "v" if ch == "و" else "y"
            options = [c] if i == 0 else [c, ""]
        elif ch in _FA:
            options = [_FA[ch]]
        elif ch.isdigit():
            options = [ch]
        else:
            continue
        readings = [r + o for r in readings for o in options][:16]
    return {_collapse(r) for r in readings}


# Words that are translated rather than transliterated in place names.
_WORD_EQUIVALENTS = {"east": "شرقی", "west": "غربی", "north": "شمالی", "south": "جنوبی"}


@lru_cache(maxsize=20_000)
def phonetic_keys(value: str) -> frozenset[str]:
    """Consonant skeletons of ``value``: one per reading, words in their order and
    sorted (so ``Razavi Khorasan`` meets ``خراسان رضوی``)."""
    words = [_WORD_EQUIVALENTS.get(w, w) for w in tokens(value)]
    if not words:
        return frozenset()
    variants = [sorted(_persian_keys(w) if is_persian(w) else {_latin_key(w)}) for w in words]
    phrases = [""]
    for options in variants:
        phrases = [f"{p} {o}".strip() for p in phrases for o in options][:32]
    keys = set(phrases)
    if len(words) > 1:
        keys |= {" ".join(sorted(p.split())) for p in phrases}
    return frozenset(k for k in keys if k.strip())


def similarity(a: str, b: str) -> float:
    """0..1 similarity of two normalised strings (``difflib`` ratio)."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def phonetic_similarity(a: str, b: str) -> float:
    """Best similarity of the transliteration keys of ``a`` and ``b`` (any script).

    Multi-word values also compare with their words sorted, so ``Razavi
    Khorasan`` meets ``خراسان رضوی``."""
    best = 0.0
    for ka in phonetic_keys(a):
        for kb in phonetic_keys(b):
            best = max(best, similarity(ka, kb))
            if " " in ka or " " in kb:
                best = max(
                    best, similarity(" ".join(sorted(ka.split())), " ".join(sorted(kb.split())))
                )
            if best == 1.0:
                return best
    return best
