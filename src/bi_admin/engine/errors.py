"""Errors raised while running a valid report."""

from __future__ import annotations

from typing import Any


class ReportError(Exception):
    """A report that is valid but cannot run (permissions, limits, missing calendar
    data). ``payload`` is the JSON error body; ``code`` never changes with the
    language."""

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = {"detail": message, "code": code, **extra}
