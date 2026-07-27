-- Core: product dimension at variant grain (the grain orders are actually
-- placed at). 24 variants across 12 products.
--
-- All prices in the source are VAT-inclusive but cost is not, so comparing
-- them directly overstates margin by the whole VAT amount. Ex-VAT price is
-- derived here, once, and unit_margin is computed from it. On this catalogue
-- that is the difference between an apparent ~78% margin and a true ~66%.

CREATE OR REPLACE TABLE core.dim_product AS
SELECT
    variant_id,
    product_id,
    sku,
    product_title,
    product_type,
    variant_title,
    product_title || ' - ' || variant_title          AS variant_label,

    price_gross,
    ROUND(price_gross / (1 + 0.20), 2)               AS price_ex_vat,
    unit_cost,
    ROUND(price_gross / (1 + 0.20) - unit_cost, 2)   AS unit_margin,
    ROUND((price_gross / (1 + 0.20) - unit_cost) / (price_gross / (1 + 0.20)), 4) AS margin_pct,

    compare_at_price,
    inventory_quantity                               AS inventory_snapshot,
    weight_grams,
    status,
    vendor
FROM staging.stg_products;
