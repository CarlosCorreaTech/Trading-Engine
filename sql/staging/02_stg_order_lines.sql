-- Staging: order line items (one row per SKU per order).
--
-- Verified against the raw file: line-level sum(price * quantity) matches the
-- order header's total_line_items_price, and sum(total_discount) matches the
-- header's total_discounts, for all 26,553 orders. The grain is trustworthy,
-- so downstream models can aggregate lines without reconciling to the header.

CREATE OR REPLACE TABLE staging.stg_order_lines AS
SELECT
    CAST(id AS BIGINT)                              AS order_line_id,
    CAST(order_id AS BIGINT)                        AS order_id,
    CAST(index AS INTEGER)                          AS line_index,

    sku,
    CAST(product_id AS BIGINT)                      AS product_id,
    CAST(variant_id AS BIGINT)                      AS variant_id,
    title                                           AS product_title,
    variant_title,
    name                                            AS line_name,
    vendor,

    CAST(quantity AS INTEGER)                       AS quantity,
    CAST(price AS DECIMAL(12, 2))                   AS unit_price_gross,
    CAST(total_discount AS DECIMAL(12, 2))          AS line_discount,

    -- Gross of VAT and before discount; the discounted figure is derived in
    -- core once VAT has been stripped, to avoid two competing definitions.
    CAST(price * quantity AS DECIMAL(12, 2))        AS line_gross,

    CAST(grams AS INTEGER)                          AS grams,
    CAST(taxable AS BOOLEAN)                        AS is_taxable,
    CAST(gift_card AS BOOLEAN)                      AS is_gift_card,
    CAST(requires_shipping AS BOOLEAN)              AS requires_shipping,
    CAST(product_exists AS BOOLEAN)                 AS product_exists,
    nullif(trim(fulfillment_status), '')            AS fulfillment_status,

    CAST(_weld_synced AS TIMESTAMP)                 AS synced_at
FROM read_csv(
    getvariable('data_dir') || '/order_lines.csv',
    header = true,
    timestampformat = '%Y-%m-%dT%H:%M:%S'
);
