-- Core: order fact, one row per order.
--
-- This is where the project commits to its revenue definition, so it is worth
-- being explicit about the three choices made here.
--
-- 1. Revenue is net of VAT. Every order has taxes_included = true, so the
--    headline total_price includes 20% VAT that the brand never keeps. All
--    downstream revenue, AOV, LTV and margin figures use net_revenue.
--
-- 2. Cancelled orders do not count. All 833 cancelled orders are also
--    financial_status = 'refunded', so the two definitions agree and there is
--    no ambiguity. is_valid_order carries this decision; nothing downstream
--    re-derives it.
--
-- 3. Fraud cancellations are flagged separately from customer cancellations.
--    They mean different things commercially: 287 fraud cancellations are a
--    payments problem, while 546 customer and other cancellations are a
--    product or fulfilment signal. Lumping them together would hide either.
--
-- Customer sequence numbers are computed over valid orders only, so a
-- cancelled first order does not consume the "first order" slot and distort
-- the acquisition cohort.

CREATE OR REPLACE TABLE core.fct_orders AS
WITH line_costs AS (
    SELECT
        l.order_id,
        SUM(l.quantity)                                       AS total_units,
        COUNT(*)                                              AS line_count,
        SUM(l.quantity * p.unit_cost)                         AS total_cogs
    FROM staging.stg_order_lines l
    JOIN core.dim_product p USING (variant_id)
    GROUP BY 1
),
base AS (
    SELECT
        o.order_id,
        o.order_name,
        o.order_number,
        o.customer_id,
        o.order_date,
        o.created_at,

        COALESCE(ch.channel, 'Unknown')                       AS channel,
        COALESCE(ch.channel_group, 'Unattributed')            AS channel_group,
        COALESCE(ch.traffic_source, 'Unknown')                AS traffic_source,
        COALESCE(ch.has_spend_data, FALSE)                    AS channel_has_spend_data,
        o.referrer_domain,
        o.landing_site,

        o.total_price_gross                                   AS gross_revenue,
        o.total_tax                                           AS vat,
        o.total_price_gross - o.total_tax                     AS net_revenue,
        o.total_discounts                                     AS discounts,
        o.shipping_price,
        o.line_items_price,

        c.total_cogs,
        o.total_price_gross - o.total_tax - COALESCE(c.total_cogs, 0) AS gross_profit,
        c.total_units,
        c.line_count,

        o.financial_status,
        o.fulfillment_status,
        o.cancel_reason,
        o.is_cancelled,
        o.is_fraud,
        NOT o.is_cancelled                                    AS is_valid_order,
        o.buyer_accepts_marketing
    FROM staging.stg_orders o
    LEFT JOIN line_costs c ON c.order_id = o.order_id
    LEFT JOIN core.dim_channel ch
           ON ch.referrer_domain IS NOT DISTINCT FROM o.referrer_domain
)
SELECT
    *,
    CASE WHEN is_valid_order THEN
        ROW_NUMBER() OVER (
            PARTITION BY customer_id, is_valid_order
            ORDER BY created_at, order_id
        )
    END                                                       AS customer_order_seq,
    CASE WHEN is_valid_order THEN
        ROW_NUMBER() OVER (
            PARTITION BY customer_id, is_valid_order
            ORDER BY created_at, order_id
        ) = 1
    END                                                       AS is_first_order
FROM base;
