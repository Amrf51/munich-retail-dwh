"""Load: staging.sales_clean -> mart dimensions and fact.

Everything here is set-based SQL. The transform stage already did the row-wise work,
so this is nine `INSERT ... SELECT` statements and nothing else. Pushing the join and
deduplication work into Postgres rather than pulling 35,612 rows back into Python is
both faster and far less code.

Two properties this module is responsible for:

  Idempotency  Every statement is an upsert on a natural key -- `store_id`,
               `product_id`, `transaction_id` -- so a second run updates in place
               rather than duplicating. No truncate-and-reload anywhere.

  Referential  Every dimension lookup falls back to the `-1` Unknown member, which
  integrity    is what lets every foreign key on the fact be NOT NULL despite 23.5%
               of source rows having no customer.

Load order is a dependency order and is not negotiable:

    dim_supplier -> dim_product      (product carries supplier_key)
    dim_store, dim_customer          (independent)
    dim_date                         (already seeded by 010_dimensions.sql)
                 -> fact_sales       (needs all five)
"""
from __future__ import annotations

from rich.console import Console

from .db import connect

console = Console()


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------
#
# Each statement uses DISTINCT ON to collapse the 35,612 staging rows down to one
# row per natural key. Without it the INSERT would fail on its own duplicates before
# ON CONFLICT ever saw them -- ON CONFLICT resolves against rows already committed in
# the table, not against duplicates inside the same statement.
#
# The ORDER BY inside DISTINCT ON decides which row wins. It is deterministic on
# purpose: an arbitrary winner would make the load non-reproducible.

DIMENSION_SQL: list[tuple[str, str]] = [
    ("dim_supplier", """
        INSERT INTO mart.dim_supplier (supplier_id, supplier_name)
        SELECT DISTINCT ON (supplier_id)
               supplier_id,
               COALESCE(supplier_name, 'Unknown')
          FROM staging.sales_clean
         WHERE supplier_id IS NOT NULL
         ORDER BY supplier_id, supplier_name
        ON CONFLICT (supplier_id) DO UPDATE
           SET supplier_name = EXCLUDED.supplier_name;
    """),

    ("dim_product", """
        INSERT INTO mart.dim_product
               (product_id, product_name, category, subcategory, brand,
                department, base_price, supplier_key)
        SELECT DISTINCT ON (s.product_id)
               s.product_id,
               COALESCE(s.product_name, 'Unknown'),
               s.category,
               s.subcategory,
               s.brand,
               -- NOT NULL in the schema. All 3,393 missing departments belong to the
               -- Miscellaneous category, so this is a structural gap, not random loss.
               COALESCE(s.department, 'Unassigned'),
               s.base_price,
               -- A product whose supplier is missing still belongs in the dimension.
               COALESCE(sup.supplier_key, -1)
          FROM staging.sales_clean s
          LEFT JOIN mart.dim_supplier sup ON sup.supplier_id = s.supplier_id
         WHERE s.product_id IS NOT NULL
         ORDER BY s.product_id, s.product_name
        ON CONFLICT (product_id) DO UPDATE
           SET product_name = EXCLUDED.product_name,
               category     = EXCLUDED.category,
               subcategory  = EXCLUDED.subcategory,
               brand        = EXCLUDED.brand,
               department   = EXCLUDED.department,
               base_price   = EXCLUDED.base_price,
               supplier_key = EXCLUDED.supplier_key;
    """),

    ("dim_store", """
        INSERT INTO mart.dim_store
               (store_id, store_name, store_type, store_location,
                district_name, postal_code, store_size_sqm, is_placeholder)
        SELECT DISTINCT ON (store_id)
               store_id,
               COALESCE(store_name, 'Unknown'),
               store_type,
               store_location,
               district_name,
               postal_code,
               -- Store 6 ships with -500 sqm, which the CHECK constraint rejects and
               -- which is not a number worth storing. Nulled rather than clamped:
               -- "unknown area" is honest, "0 sqm" would be a fabricated fact.
               NULLIF(GREATEST(store_size_sqm, 0), 0),
               -- Derived from the transform's flag rather than re-deriving the rule
               -- here, so the definition of "placeholder" lives in exactly one place.
               BOOL_OR((dq_flags & 16) > 0) OVER (PARTITION BY store_id)
          FROM staging.sales_clean
         WHERE store_id IS NOT NULL
         ORDER BY store_id, store_name
        ON CONFLICT (store_id) DO UPDATE
           SET store_name     = EXCLUDED.store_name,
               store_type     = EXCLUDED.store_type,
               store_location = EXCLUDED.store_location,
               district_name  = EXCLUDED.district_name,
               postal_code    = EXCLUDED.postal_code,
               store_size_sqm = EXCLUDED.store_size_sqm,
               is_placeholder = EXCLUDED.is_placeholder;
    """),

    ("dim_customer", """
        INSERT INTO mart.dim_customer
               (customer_id, customer_name, email_domain, loyalty_status)
        SELECT DISTINCT ON (customer_id)
               customer_id,
               COALESCE(customer_name, 'Unnamed'),
               email_domain,
               COALESCE(loyalty_status, 'Unknown')
          FROM staging.sales_clean
         WHERE customer_id IS NOT NULL
         -- NULLS LAST so a row that actually carries a loyalty tier wins over one
         -- that does not. 2,478 rows have a customer id but no tier.
         ORDER BY customer_id, loyalty_status NULLS LAST, email_domain NULLS LAST
        ON CONFLICT (customer_id) DO UPDATE
           SET customer_name  = EXCLUDED.customer_name,
               email_domain   = EXCLUDED.email_domain,
               loyalty_status = EXCLUDED.loyalty_status;
    """),
]


# ---------------------------------------------------------------------------
# Fact
# ---------------------------------------------------------------------------
#
# LEFT JOIN plus COALESCE to -1 on every dimension lookup. That combination is what
# makes the NOT NULL foreign keys survivable: a row with no customer gets the Unknown
# member rather than failing the insert or forcing the column to be nullable.
#
# The conflict target is transaction_id, which is genuinely unique across all 35,612
# source rows. That makes this a true upsert -- a re-run updates rows in place, so
# there is never a moment where the fact table is empty or half-populated.

FACT_SQL = """
INSERT INTO mart.fact_sales (
    transaction_id, invoice_number,
    date_key, store_key, product_key, customer_key,
    transaction_time, transaction_status, payment_method,
    sales_staff_name, promotion_name,
    quantity, unit_price, base_price, discount_rate,
    tax_rate, tax_amount, total_amount, total_amount_raw,
    dq_flags, batch_id
)
SELECT
    s.transaction_id,
    s.invoice_number,
    COALESCE(d.date_key,     -1),
    COALESCE(st.store_key,   -1),
    COALESCE(p.product_key,  -1),
    COALESCE(c.customer_key, -1),
    s.transaction_time,
    COALESCE(s.transaction_status, 'Unknown'),
    s.payment_method,
    s.sales_staff_name,
    s.promotion_name,
    s.quantity,
    s.unit_price,
    s.base_price,
    s.discount_rate,
    s.tax_rate,
    s.tax_amount,
    s.total_amount,
    s.total_amount_raw,
    s.dq_flags,
    s.batch_id
FROM staging.sales_clean s
LEFT JOIN mart.dim_date     d  ON d.full_date   = s.transaction_date
LEFT JOIN mart.dim_store    st ON st.store_id   = s.store_id
LEFT JOIN mart.dim_product  p  ON p.product_id  = s.product_id
LEFT JOIN mart.dim_customer c  ON c.customer_id = s.customer_id
ON CONFLICT (transaction_id) DO UPDATE SET
    invoice_number     = EXCLUDED.invoice_number,
    date_key           = EXCLUDED.date_key,
    store_key          = EXCLUDED.store_key,
    product_key        = EXCLUDED.product_key,
    customer_key       = EXCLUDED.customer_key,
    transaction_time   = EXCLUDED.transaction_time,
    transaction_status = EXCLUDED.transaction_status,
    payment_method     = EXCLUDED.payment_method,
    sales_staff_name   = EXCLUDED.sales_staff_name,
    promotion_name     = EXCLUDED.promotion_name,
    quantity           = EXCLUDED.quantity,
    unit_price         = EXCLUDED.unit_price,
    base_price         = EXCLUDED.base_price,
    discount_rate      = EXCLUDED.discount_rate,
    tax_rate           = EXCLUDED.tax_rate,
    tax_amount         = EXCLUDED.tax_amount,
    total_amount       = EXCLUDED.total_amount,
    total_amount_raw   = EXCLUDED.total_amount_raw,
    dq_flags           = EXCLUDED.dq_flags,
    batch_id           = EXCLUDED.batch_id,
    loaded_at          = now();
"""


# ---------------------------------------------------------------------------

def load_dimensions() -> dict[str, int]:
    """Upsert all four source-driven dimensions. dim_date is seeded by migration."""
    counts: dict[str, int] = {}
    # One transaction for all four: a half-loaded set of dimensions would leave the
    # fact load resolving keys against dimensions that do not yet exist.
    with connect() as conn, conn.cursor() as cur:
        for name, statement in DIMENSION_SQL:
            cur.execute(statement)
            counts[name] = cur.rowcount
            console.print(f"[green]+[/green] {name}: {cur.rowcount:,} rows upserted")
        conn.commit()
    return counts


def load_fact() -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(FACT_SQL)
        rows = cur.rowcount
        # Fresh statistics, so the planner does not treat a freshly loaded table as
        # empty and choose a sequential scan for every query afterwards.
        cur.execute("ANALYZE mart.fact_sales")
        conn.commit()
    console.print(f"[green]+[/green] fact_sales: {rows:,} rows upserted")
    return rows


def load_all() -> dict[str, int]:
    counts = load_dimensions()
    counts["fact_sales"] = load_fact()
    return counts


def mart_summary() -> list[dict]:
    """Row counts and Unknown-member usage, for the console summary."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT 'dim_date'     AS table_name, COUNT(*) AS rows FROM mart.dim_date
            UNION ALL SELECT 'dim_store',    COUNT(*) FROM mart.dim_store
            UNION ALL SELECT 'dim_product',  COUNT(*) FROM mart.dim_product
            UNION ALL SELECT 'dim_supplier', COUNT(*) FROM mart.dim_supplier
            UNION ALL SELECT 'dim_customer', COUNT(*) FROM mart.dim_customer
            UNION ALL SELECT 'fact_sales',   COUNT(*) FROM mart.fact_sales
        """)
        return cur.fetchall()


def unknown_member_usage() -> list[dict]:
    """How many fact rows resolved to an Unknown member, per dimension.

    Worth surfacing rather than hiding: a spike here means a dimension load silently
    stopped matching, which would otherwise show up much later as missing revenue.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT 'customer_key' AS dimension,
                   COUNT(*) FILTER (WHERE customer_key = -1) AS unknown_rows,
                   COUNT(*)                                  AS total_rows
              FROM mart.fact_sales
            UNION ALL SELECT 'store_key',
                   COUNT(*) FILTER (WHERE store_key = -1),   COUNT(*) FROM mart.fact_sales
            UNION ALL SELECT 'product_key',
                   COUNT(*) FILTER (WHERE product_key = -1), COUNT(*) FROM mart.fact_sales
            UNION ALL SELECT 'date_key',
                   COUNT(*) FILTER (WHERE date_key = -1),    COUNT(*) FROM mart.fact_sales
            ORDER BY 2 DESC
        """)
        return cur.fetchall()