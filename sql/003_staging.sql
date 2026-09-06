-- Typed, standardised, flagged. One row per source row, shaped to match the mart
-- so the dimension and fact loaders are plain INSERT ... SELECT statements.
CREATE TABLE IF NOT EXISTS staging.sales_clean (
    -- TEXT, not UUID. 689 of 35,612 source ids are 10-character alphanumeric strings
    -- rather than UUIDs. All 35,612 are unique, so they are valid keys in a second
    -- format; typing this UUID would reject 689 good rows over cosmetics.
    transaction_id     TEXT PRIMARY KEY,
    batch_id           BIGINT REFERENCES meta.load_batch(batch_id),
    invoice_number     TEXT,

    transaction_date   DATE NOT NULL,
    transaction_time   TIME,
    transaction_status TEXT NOT NULL,

    store_id           INT  NOT NULL,
    store_name         TEXT,
    store_type         TEXT,
    store_location     TEXT,
    district_name      TEXT,
    postal_code        TEXT,
    store_size_sqm     INT,

    customer_id        INT,
    customer_name      TEXT,
    email_domain       TEXT,
    loyalty_status     TEXT,

    product_id         INT  NOT NULL,
    product_name       TEXT,
    category           TEXT,
    subcategory        TEXT,
    brand              TEXT,
    department         TEXT,
    base_price         NUMERIC(12,2),

    supplier_id        INT,
    supplier_name      TEXT,

    quantity           INT,
    unit_price         NUMERIC(12,2),
    discount_rate      NUMERIC(6,4),
    tax_rate           NUMERIC(5,4),
    tax_amount         NUMERIC(14,2),
    total_amount       NUMERIC(14,2),
    total_amount_raw   NUMERIC(14,2),

    payment_method     TEXT,
    sales_staff_name   TEXT,
    promotion_name     TEXT,

    dq_flags           INT  NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_stg_sales_date  ON staging.sales_clean (transaction_date);
CREATE INDEX IF NOT EXISTS ix_stg_sales_flags ON staging.sales_clean (dq_flags) WHERE dq_flags <> 0;
