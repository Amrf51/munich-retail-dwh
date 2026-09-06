-- Wide, pre-joined view. The snowflake arm to dim_supplier is hidden here, so
-- nobody has to think about it for ad-hoc work.
CREATE OR REPLACE VIEW mart.vw_sales_enriched AS
SELECT
    f.sale_key,
    f.transaction_id,
    f.invoice_number,

    d.full_date,
    d.year, d.quarter, d.month, d.month_name, d.iso_week,
    d.day_name, d.is_weekend, d.is_bavarian_holiday,
    f.transaction_time,
    EXTRACT(HOUR FROM f.transaction_time)::SMALLINT AS hour_of_day,

    f.transaction_status,
    -- The business rule lives here, once, instead of in every query: cancelled and
    -- returned lines are kept for return-rate analysis but never count as revenue.
    (f.transaction_status = 'Completed') AS is_revenue_recognized,

    s.store_name, s.store_type, s.store_location,
    s.district_name, s.postal_code, s.store_size_sqm,
    s.is_placeholder AS store_is_placeholder,

    p.product_name, p.category, p.subcategory, p.brand, p.department,
    p.base_price AS product_base_price,
    sup.supplier_name,

    c.customer_name, c.email_domain, c.loyalty_status,
    (c.customer_key = -1) AS is_walk_in,

    f.payment_method, f.sales_staff_name, f.promotion_name,

    f.quantity, f.unit_price, f.discount_rate,
    f.tax_amount, f.total_amount, f.total_amount_raw,

    f.dq_flags,
    (f.dq_flags = 0) AS is_clean
FROM mart.fact_sales    f
JOIN mart.dim_date      d   ON d.date_key      = f.date_key
JOIN mart.dim_store     s   ON s.store_key     = f.store_key
JOIN mart.dim_product   p   ON p.product_key   = f.product_key
JOIN mart.dim_supplier  sup ON sup.supplier_key = p.supplier_key
JOIN mart.dim_customer  c   ON c.customer_key  = f.customer_key;

-- What reporting should actually use. Revenue-recognised lines only, with the
-- known-bad rows held back: bit 1 total mismatch, 2 quantity outlier,
-- 4 discount out of range, 16 placeholder store.
CREATE OR REPLACE VIEW mart.vw_net_revenue AS
SELECT *
FROM mart.vw_sales_enriched
WHERE is_revenue_recognized
  AND (dq_flags & (1 | 2 | 4 | 16)) = 0;

-- The rows vw_net_revenue deliberately hides. Keeping them loadable is the whole
-- reason return-rate analysis is possible at all.
CREATE OR REPLACE VIEW mart.vw_returns_and_cancellations AS
SELECT *
FROM mart.vw_sales_enriched
WHERE NOT is_revenue_recognized;

-- Flag distribution across the fact, for the quality report.
CREATE OR REPLACE VIEW mart.vw_dq_summary AS
SELECT flag_name, rows_flagged,
       ROUND(100.0 * rows_flagged / NULLIF((SELECT COUNT(*) FROM mart.fact_sales), 0), 2) AS pct_of_fact
FROM (
    SELECT 'total_mismatch' AS flag_name,          COUNT(*) FILTER (WHERE dq_flags &    1 > 0) AS rows_flagged FROM mart.fact_sales
    UNION ALL SELECT 'quantity_outlier',           COUNT(*) FILTER (WHERE dq_flags &    2 > 0) FROM mart.fact_sales
    UNION ALL SELECT 'discount_out_of_range',      COUNT(*) FILTER (WHERE dq_flags &    4 > 0) FROM mart.fact_sales
    UNION ALL SELECT 'negative_qty_on_completed',  COUNT(*) FILTER (WHERE dq_flags &    8 > 0) FROM mart.fact_sales
    UNION ALL SELECT 'placeholder_store',          COUNT(*) FILTER (WHERE dq_flags &   16 > 0) FROM mart.fact_sales
    UNION ALL SELECT 'repaired_from_na',           COUNT(*) FILTER (WHERE dq_flags &   32 > 0) FROM mart.fact_sales
    UNION ALL SELECT 'missing_customer',           COUNT(*) FILTER (WHERE dq_flags &   64 > 0) FROM mart.fact_sales
    UNION ALL SELECT 'discount_flag_conflict',     COUNT(*) FILTER (WHERE dq_flags &  128 > 0) FROM mart.fact_sales
    UNION ALL SELECT 'unparsed_date',              COUNT(*) FILTER (WHERE dq_flags &  256 > 0) FROM mart.fact_sales
    UNION ALL SELECT 'non_uuid_transaction_id',    COUNT(*) FILTER (WHERE dq_flags &  512 > 0) FROM mart.fact_sales
    UNION ALL SELECT 'unparseable_email_domain',   COUNT(*) FILTER (WHERE dq_flags & 1024 > 0) FROM mart.fact_sales
) t
ORDER BY rows_flagged DESC;
