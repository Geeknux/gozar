"""The Persian gettext catalogue of bi_admin is complete: every string the code marks
for translation has a Persian translation that isn't fuzzy, and the compiled .mo
matches the .po. After changing strings, from src/bi_admin:

    django-admin makemessages -l fa   (then translate what it added or marked fuzzy)
    django-admin compilemessages -l fa
"""

import ast
import gettext
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "src" / "bi_admin"
PO = APP / "locale" / "fa" / "LC_MESSAGES" / "django.po"
MO = PO.with_suffix(".mo")


def entries(path):
    """(msgctxt, msgid, msgid_plural, [msgstr...], flags) for each live entry of a .po file."""
    out = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8")):
        lines = [line for line in block.splitlines() if not line.startswith("#~")]
        flags = {
            flag.strip() for line in lines if line.startswith("#,") for flag in line[2:].split(",")
        }
        fields, current = {}, None
        for line in lines:
            if line.startswith("#") or not line.strip():
                continue
            if line.startswith('"'):
                fields[current] += ast.literal_eval(line)
            else:
                current, value = line.split(" ", 1)
                fields[current] = ast.literal_eval(value)
        if "msgid" not in fields or not fields["msgid"]:
            continue  # the header, or a block of obsolete entries
        msgstrs = [v for k, v in sorted(fields.items()) if k.startswith("msgstr")]
        out.append(
            (fields.get("msgctxt"), fields["msgid"], fields.get("msgid_plural"), msgstrs, flags)
        )
    return out


def gaps(path):
    """The msgids that are untranslated (or partly, for plurals) or fuzzy."""
    return [
        ("fuzzy: " if "fuzzy" in flags else "untranslated: ") + msgid
        for _, msgid, _, msgstrs, flags in entries(path)
        if "fuzzy" in flags or not msgstrs or not all(msgstrs)
    ]


def test_persian_catalogue_has_no_untranslated_or_fuzzy_entries():
    assert entries(PO), "the parser found no entries"
    assert gaps(PO) == []


def test_compiled_catalogue_matches_the_po_file():
    catalog = gettext.GNUTranslations(MO.open("rb"))
    stale = []
    for context, msgid, plural, msgstrs, _ in entries(PO):
        if plural:
            got = [
                catalog.npgettext(context, msgid, plural, n)
                if context
                else catalog.ngettext(msgid, plural, n)
                for n in (1, 2)
            ]
        else:
            got = [catalog.pgettext(context, msgid) if context else catalog.gettext(msgid)]
        if got != msgstrs:
            stale.append(msgid)
    assert stale == [], "run `django-admin compilemessages -l fa` in src/bi_admin"


@pytest.mark.skipif(
    not shutil.which("xgettext") and not os.environ.get("CI"),
    reason="needs GNU gettext (xgettext, msgmerge); CI installs it",
)
def test_every_translatable_string_in_the_code_is_in_the_persian_catalogue(tmp_path):
    """makemessages on a copy of the app: a string added or changed in the code without
    updating the catalogue shows up as untranslated or fuzzy."""
    copy = tmp_path / "bi_admin"
    shutil.copytree(APP, copy, ignore=shutil.ignore_patterns("static", "__pycache__"))
    django_admin = [sys.executable, "-m", "django"]
    subprocess.run(
        [*django_admin, "makemessages", "-l", "fa", "--no-obsolete"],
        cwd=copy,
        check=True,
        capture_output=True,
        env={k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"},
    )
    missing = gaps(copy / "locale" / "fa" / "LC_MESSAGES" / "django.po")
    assert missing == [], "run `django-admin makemessages -l fa` in src/bi_admin and translate"
