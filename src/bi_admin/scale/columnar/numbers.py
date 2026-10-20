"""Measure values as the ORM engine returns them from the primary database.

A columnar engine computes exact sums and counts; what the ORM engine returns for
the same aggregate depends on the primary database and on Django's converters:

* **PostgreSQL** returns ``numeric`` exactly, and ``AVG`` is ``numeric_div`` of
  the exact sum by the count, rounded to the scale ``select_div_scale`` picks
  (at least 16 significant digits). ``pg_div`` reproduces that division.
* **SQLite** sums integers exactly and divides as ``double`` for ``avg()``; Django
  then turns a ``DecimalField`` aggregate into ``Decimal`` through a 15-digit
  context (``create_decimal_from_float``), even for exact integer sums.
"""

from __future__ import annotations

import decimal
from typing import Any

from django.db import models

_SQLITE_DECIMAL = decimal.Context(prec=15).create_decimal_from_float
NUMERIC_MIN_SIG_DIGITS = 16
DEC_DIGITS = 4  # PostgreSQL stores numerics in base 10 000


def _weight(value: decimal.Decimal) -> tuple[int, int]:
    """PostgreSQL's (weight, first base-10000 digit) of a non-zero numeric."""
    value = abs(value)
    weight = value.adjusted() // DEC_DIGITS
    first = int(value.scaleb(-DEC_DIGITS * weight))
    return weight, first


def pg_div(a: decimal.Decimal, b: decimal.Decimal, scale_a: int, scale_b: int = 0):
    """``a / b`` as PostgreSQL's ``numeric_div`` computes it (``b`` ≠ 0)."""
    w1, f1 = _weight(a) if a else (0, 0)
    w2, f2 = _weight(b) if b else (0, 0)
    qweight = w1 - w2
    if f1 <= f2:
        qweight -= 1
    rscale = NUMERIC_MIN_SIG_DIGITS - qweight * DEC_DIGITS
    rscale = min(max(rscale, scale_a, scale_b, 0), 1000)
    # exact: round(a / b * 10^rscale) half away from zero, in integers
    sa, da, ea = a.as_tuple()
    sb, db, eb = b.as_tuple()
    num = int("".join(map(str, da)) or "0")
    den = int("".join(map(str, db)) or "0")
    shift = ea - eb + rscale
    if shift >= 0:
        num *= 10**shift
    else:
        den *= 10**-shift
    quotient, remainder = divmod(num, den)
    if 2 * remainder >= den:
        quotient += 1
    negative = (sa == 1) != (sb == 1) and quotient != 0
    return decimal.Decimal((1 if negative else 0, tuple(map(int, str(quotient))), -rscale))


def as_orm(value: Any, output: models.Field, vendor: str) -> Any:
    """A sum/min/max/count from the columnar engine as the ORM engine would return it."""
    if value is None:
        return None
    if vendor == "sqlite" and output.get_internal_type() == "DecimalField":
        if isinstance(value, decimal.Decimal):
            value = int(value) if value == value.to_integral_value() else float(value)
        return _SQLITE_DECIMAL(value)
    return value


def average(total: Any, count: int, output: models.Field, scale: int, vendor: str) -> Any:
    """``AVG`` from an exact ``total`` and ``count`` as the primary database computes
    it (``scale``: the decimal places of the summed column)."""
    if not count or total is None:
        return None
    if vendor == "postgresql":
        return pg_div(decimal.Decimal(total), decimal.Decimal(count), scale, 0)
    result = float(total) / float(count)  # SQLite: double division of the exact sum
    if output.get_internal_type() == "DecimalField":
        return _SQLITE_DECIMAL(result)
    return result
