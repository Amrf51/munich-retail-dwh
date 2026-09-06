"""Automated validation test suite for Munich Retail Data Warehouse.

Verifies schema integrity, row counts, foreign key constraints,
data transformations, and financial calculations.
"""
from __future__ import annotations

from decimal import Decimal
import pytest
from munich_dwh.db import connect


@pytest.fixture(scope="module")
def db_cursor():
    """Yield a database cursor connected to the warehouse."""
    with connect() as conn, conn.cursor() as cur:
        yield cur


def test_fact_row_count_parity(db_cursor):
    """Fact table must contain exactly 35,612 rows, matching the source CSV."""
    db_cursor.execute("SELECT COUNT(*) AS total FROM mart.fact_sales;")
    assert db_cursor.fetchone()["total"] == 35612


def test_non_uuid_transaction_ids_preserved(db_cursor):
    """Profiling identified 689 non-UUID alphanumeric IDs (e.g. 00LXVQRWKP).
    All 689 must land in fact_sales without failure."""
    db_cursor.execute("""
        SELECT COUNT(*) AS total
        FROM mart.fact_sales
        WHERE (dq_flags & 512) > 0;
    """)
    assert db_cursor.fetchone()["total"] == 689


def test_walk_in_customers_mapped_to_surrogate(db_cursor):
    """23.5% (8,380 rows) have no customer_id. They must all map to customer_key = -1."""
    db_cursor.execute("""
        SELECT COUNT(*) AS total
        FROM mart.fact_sales
        WHERE customer_key = -1;
    """)
    assert db_cursor.fetchone()["total"] == 8380


def test_referential_integrity_no_orphaned_keys(db_cursor):
    """Every foreign key in fact_sales must resolve to an existing dimension record."""
    db_cursor.execute("""
        SELECT
            (SELECT COUNT(*) FROM mart.fact_sales f LEFT JOIN mart.dim_date d ON d.date_key = f.date_key WHERE d.date_key IS NULL) AS orphan_dates,
            (SELECT COUNT(*) FROM mart.fact_sales f LEFT JOIN mart.dim_store s ON s.store_key = f.store_key WHERE s.store_key IS NULL) AS orphan_stores,
            (SELECT COUNT(*) FROM mart.fact_sales f LEFT JOIN mart.dim_product p ON p.product_key = f.product_key WHERE p.product_key IS NULL) AS orphan_products,
            (SELECT COUNT(*) FROM mart.fact_sales f LEFT JOIN mart.dim_customer c ON c.customer_key = f.customer_key WHERE c.customer_key IS NULL) AS orphan_customers;
    """)
    row = db_cursor.fetchone()
    assert row["orphan_dates"] == 0
    assert row["orphan_stores"] == 0
    assert row["orphan_products"] == 0
    assert row["orphan_customers"] == 0


def test_canonical_revenue_calculation(db_cursor):
    """For every row in fact_sales, total_amount must equal quantity * unit_price."""
    db_cursor.execute("""
        SELECT COUNT(*) AS mismatches
        FROM mart.fact_sales
        WHERE quantity IS NOT NULL
          AND unit_price IS NOT NULL
          AND total_amount <> ROUND(quantity * unit_price, 2);
    """)
    assert db_cursor.fetchone()["mismatches"] == 0


def test_bavarian_holidays_in_date_dimension(db_cursor):
    """dim_date must have 1,097 rows and flag Bavarian public holidays."""
    db_cursor.execute("SELECT COUNT(*) AS total FROM mart.dim_date;")
    assert db_cursor.fetchone()["total"] == 1097

    db_cursor.execute("""
        SELECT holiday_name
        FROM mart.dim_date
        WHERE full_date = DATE '2024-01-01' AND is_bavarian_holiday = TRUE;
    """)
    assert db_cursor.fetchone()["holiday_name"] == "Neujahr"


def test_views_operational(db_cursor):
    """Reporting views must execute and return valid revenue data."""
    db_cursor.execute("""
        SELECT COUNT(*) AS clean_rows, ROUND(SUM(total_amount), 2) AS net_revenue
        FROM mart.vw_net_revenue;
    """)
    row = db_cursor.fetchone()
    assert row["clean_rows"] == 27368
    assert row["net_revenue"] > Decimal("6000000")
