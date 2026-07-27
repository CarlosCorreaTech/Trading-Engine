-- Core: order line fact, one row per SKU per order.
--
-- The source gives VAT only at order level, so per-line VAT has to be derived.
-- Checking how the header actually computes it first: total_tax equals
-- subtotal_price / 6 on every order, which means VAT is charged on the
-- discounted goods value and shipping is not taxed at all. Dividing the
-- discounted line value by 1.20 therefore reproduces the header exactly, and
-- the line sum ties to net_revenue minus the untaxed shipping on all 26,553
-- orders (worst case 2p, total drift 74 GBP on 1.26m, all penny rounding).
--
-- Had shipping been assumed VAT-bearing, only 15,809 orders would reconcile.
-- The DQ layer asserts this tie-out so the assumption cannot rot silently.
--
-- Carrying is_valid_order down from the order header means SKU-level analysis
-- applies the same cancelled-order rule as revenue reporting, instead of each
-- query inventing its own filter and quietly disagreeing.

CREATE OR REPLACE TABLE core.fct_order_lines AS
SELECT
    l.order_line_id,
    l.order_id,
    l.line_index,
    o.order_date,
    o.customer_id,
    o.channel,

    l.variant_id,
    l.product_id,
    l.sku,
    p.product_title,
    p.variant_title,
    p.variant_label,
    p.product_type,

    l.quantity,
    l.unit_price_gross,
    l.line_gross,
    l.line_discount,
    l.line_gross - l.line_discount                                   AS line_net_gross_of_vat,
    ROUND((l.line_gross - l.line_discount) / (1 + 0.20), 2)          AS line_net_revenue,
    ROUND(l.quantity * p.unit_cost, 2)                               AS line_cogs,
    ROUND((l.line_gross - l.line_discount) / (1 + 0.20)
          - l.quantity * p.unit_cost, 2)                             AS line_gross_profit,

    o.is_valid_order,
    o.is_first_order,
    o.customer_order_seq
FROM staging.stg_order_lines l
JOIN core.fct_orders o USING (order_id)
JOIN core.dim_product p USING (variant_id);
