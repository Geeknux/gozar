"""The columnar engine's numbers equal what the primary database returns."""

import decimal
import random

import pytest
from django.db import connection, models

from bi_admin.scale.columnar.numbers import as_orm, average, pg_div

pytestmark = pytest.mark.django_db


@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL's numeric division")
def test_pg_div_matches_postgres_avg_digit_for_digit():
    rng = random.Random(1)
    with connection.cursor() as cursor:
        for _ in range(400):
            scale = rng.choice([0, 0, 2, 3])
            mag = rng.choice([1, 10, 1000, 10**6, 10**12])
            values = [
                decimal.Decimal(rng.randint(-mag, mag * 5)).scaleb(-scale)
                for _ in range(rng.randint(1, 30))
            ]
            rows = ", ".join(["(%s::numeric(30, %s))"] * len(values))
            params = [p for v in values for p in (v, scale)]
            cursor.execute(f"SELECT AVG(x), SUM(x), COUNT(x) FROM (VALUES {rows}) t(x)", params)
            avg, total, count = cursor.fetchone()
            mine = pg_div(total, decimal.Decimal(count), scale)
            assert (mine, str(mine)) == (avg, str(avg))


def test_sqlite_conversions():
    decimal_field = models.DecimalField(max_digits=16, decimal_places=0)
    # Django's SQLite converter: a 15-digit context, even for exact integer sums
    assert as_orm(decimal.Decimal("1234567890123456789"), decimal_field, "sqlite") == (
        decimal.Decimal("1.23456789012346E+18")
    )
    assert as_orm(decimal.Decimal(300), decimal_field, "postgresql") == decimal.Decimal(300)
    assert average(decimal.Decimal(10), 3, decimal_field, 0, "sqlite") == decimal.Decimal(
        "3.33333333333333"
    )
    assert average(decimal.Decimal(10), 3, models.FloatField(), 0, "sqlite") == 10 / 3
    assert average(None, 0, decimal_field, 0, "sqlite") is None
