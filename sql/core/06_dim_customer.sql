-- Core: customer dimension with recomputed lifetime metrics.
--
-- Every lifetime figure here is derived from core.fct_orders rather than taken
-- from the customer export, because the export's two pre-aggregated columns
-- disagree with each other: total_spent excludes cancelled orders but
-- order_count includes them, so 825 customers carry a count that does not
-- match their spend. The raw values are retained alongside purely so the DQ
-- layer can quantify the drift.
--
-- Acquisition attributes (cohort month, first channel, first product) are
-- fixed at the first valid order and stored here, so that cohort analysis
-- always slices on the same definition. First-touch channel is a deliberate
-- choice for acquisition: the channel that produced the first purchase is the
-- one that earned the customer, even though later orders may arrive elsewhere.
--
-- 533 customers have no valid order at all. They are subscribers or prospects,
-- kept in the dimension (they are real marketable records) but excluded from
-- any per-customer average via has_purchased, so AOV and LTV are not diluted
-- by people who never bought.

CREATE OR REPLACE TABLE core.dim_customer AS
WITH order_stats AS (
    SELECT
        customer_id,
        COUNT(*)                                        AS orders_count,
        SUM(net_revenue)                                AS lifetime_revenue,
        SUM(gross_profit)                               AS lifetime_gross_profit,
        SUM(total_units)                                AS lifetime_units,
        MIN(order_date)                                 AS first_order_date,
        MAX(order_date)                                 AS last_order_date,
        AVG(net_revenue)                                AS avg_order_value
    FROM core.fct_orders
    WHERE is_valid_order
    GROUP BY 1
),
first_order AS (
    SELECT
        customer_id,
        channel                                         AS first_order_channel,
        channel_group                                   AS first_order_channel_group,
        order_id                                        AS first_order_id,
        net_revenue                                     AS first_order_value
    FROM core.fct_orders
    WHERE is_valid_order AND is_first_order
),
first_product AS (
    -- The SKU that opened the relationship. Needed to test whether the
    -- customers acquired by a surging low-price product go on to repeat.
    SELECT customer_id, product_title AS first_product_title, sku AS first_sku
    FROM (
        SELECT
            l.customer_id,
            l.product_title,
            l.sku,
            ROW_NUMBER() OVER (
                PARTITION BY l.customer_id
                ORDER BY l.line_net_revenue DESC, l.line_index
            ) AS rn
        FROM core.fct_order_lines l
        WHERE l.is_valid_order AND l.is_first_order
    )
    WHERE rn = 1
),
cancelled_stats AS (
    SELECT customer_id, COUNT(*) AS cancelled_orders_count
    FROM core.fct_orders
    WHERE is_cancelled
    GROUP BY 1
)
SELECT
    c.customer_id,
    c.email,
    c.email_domain,
    c.signup_date,
    c.created_at                                        AS signed_up_at,
    c.account_state,
    c.is_verified_email,
    c.accepts_marketing,
    c.consent_state,

    COALESCE(s.orders_count, 0)                         AS orders_count,
    COALESCE(cx.cancelled_orders_count, 0)              AS cancelled_orders_count,
    COALESCE(s.lifetime_revenue, 0)                     AS lifetime_revenue,
    COALESCE(s.lifetime_gross_profit, 0)                AS lifetime_gross_profit,
    COALESCE(s.lifetime_units, 0)                       AS lifetime_units,
    s.avg_order_value,
    s.first_order_date,
    s.last_order_date,
    date_trunc('month', s.first_order_date)::DATE       AS cohort_month,
    (s.last_order_date - s.first_order_date)            AS customer_tenure_days,

    f.first_order_id,
    f.first_order_value,
    COALESCE(f.first_order_channel, 'Unknown')          AS first_order_channel,
    COALESCE(f.first_order_channel_group, 'Unattributed') AS first_order_channel_group,
    fp.first_product_title,
    fp.first_sku,

    (s.orders_count IS NOT NULL)                        AS has_purchased,
    (COALESCE(s.orders_count, 0) > 1)                   AS is_repeat_customer,

    -- Retained only for the DQ reconciliation; do not use these for analysis.
    c.order_count_raw,
    c.total_spent_raw
FROM staging.stg_customers c
LEFT JOIN order_stats s     ON s.customer_id = c.customer_id
LEFT JOIN first_order f     ON f.customer_id = c.customer_id
LEFT JOIN first_product fp  ON fp.customer_id = c.customer_id
LEFT JOIN cancelled_stats cx ON cx.customer_id = c.customer_id;
