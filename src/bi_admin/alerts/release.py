"""The release an alert was raised in: ``GOZAR["ALERTS"]["RELEASE"]``, else the
``GOZAR_RELEASE`` environment variable, else Gozar's version plus the short git
sha of the checkout (read from ``.git`` without running git)."""

from __future__ import annotations

import functools
import os
from pathlib import Path

import bi_admin


def _git_dir() -> Path | None:
    from django.conf import settings

    starts = [Path(bi_admin.__file__).resolve().parent]
    base = getattr(settings, "BASE_DIR", None)
    if base:
        starts.append(Path(base).resolve())
    for start in starts:
        for folder in [start, *start.parents]:
            candidate = folder / ".git"
            if candidate.is_dir():
                return candidate
    return None


def _sha(git: Path) -> str:
    head = (git / "HEAD").read_text().strip()
    if not head.startswith("ref:"):
        return head
    ref = head.split(":", 1)[1].strip()
    loose = git / ref
    if loose.is_file():
        return loose.read_text().strip()
    packed = git / "packed-refs"
    if packed.is_file():
        for line in packed.read_text().splitlines():
            if line.endswith(f" {ref}"):
                return line.split(" ", 1)[0]
    return ""


@functools.cache
def git_sha() -> str:
    try:
        git = _git_dir()
        return _sha(git)[:12] if git else ""
    except OSError:
        return ""


def release() -> str:
    from bi_admin.alerts.conf import alert_setting

    configured = alert_setting("RELEASE") or os.environ.get("GOZAR_RELEASE")
    if configured:
        return str(configured)[:80]
    sha = git_sha()
    return f"{bi_admin.__version__}+{sha}" if sha else bi_admin.__version__
