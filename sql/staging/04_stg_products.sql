-- Staging: product catalogue at variant grain (24 variants across 12 products).
--
-- inventory_quantity is a current snapshot with no history, which constrains
-- what the engine can honestly claim: days-of-cover can be projected forward
-- from today's stock and recent velocity, but past stockouts are unknowable.
-- The inventory recommendation is framed accordingly.
--
-- compare_at_price is populated for only 7 of 24 variants, and in every case it
-- is exactly price / 0.8. That is a standing "20% off the larger size" anchor
-- on the bigger pack of each product, not a live promotion, so it cannot be
-- used to measure promotional depth over time. Discount analysis uses the
-- order-level discounts actually applied instead.

CREATE OR REPLACE TABLE staging.stg_products AS
SELECT
    CAST(product_id AS BIGINT)                      AS product_id,
    CAST(variant_id AS BIGINT)                      AS variant_id,
    sku,
    product_title,
    product_type,
    variant_title,

    CAST(price AS DECIMAL(12, 2))                   AS price_gross,
    CAST(compare_at_price AS DECIMAL(12, 2))        AS compare_at_price,
    CAST(cost AS DECIMAL(12, 2))                    AS unit_cost,

    CAST(weight_grams AS INTEGER)                   AS weight_grams,
    CAST(inventory_quantity AS INTEGER)             AS inventory_quantity,
    status,
    vendor
FROM read_csv(getvariable('data_dir') || '/products.csv', header = true);
