-- Semantic: acquisition cohort retention triangle.
--
-- Cohorts are keyed on the month of a customer's first valid order, and
-- months_since counts calendar months from that point.
--
-- is_mature is the column that stops this table lying. A cohort acquired in
-- June 2025 has had one month to come back; one acquired in July 2024 has had
-- twelve. Comparing their 6-month retention would show a "collapse" that is
-- purely the calendar running out. Any comparison across cohorts must filter
-- on is_mature, and the DQ layer asserts that the engine does.
--
-- Cohort sizes are also wildly uneven (1,197 in January against 2,361 in
-- December), so retention_rate is the only comparable measure here and
-- active_customers on its own is not.

CREATE OR REPLACE TABLE semantic.cohort_retention AS
WITH cohort_sizes AS (
    SELECT cohort_month, COUNT(*) AS cohort_size
    FROM core.dim_customer
    WHERE has_purchased
    GROUP BY 1
),
activity AS (
    SELECT
        c.cohort_month,
        date_diff('month', c.cohort_month, date_trunc('month', o.order_date)::DATE) AS months_since,
        COUNT(DISTINCT o.customer_id)                   AS active_customers,
        COUNT(*)                                        AS orders,
        SUM(o.net_revenue)                              AS net_revenue,
        SUM(o.gross_profit)                             AS gross_profit
    FROM core.fct_orders o
    JOIN core.dim_customer c USING (customer_id)
    WHERE o.is_valid_order AND c.has_purchased
    GROUP BY 1, 2
),
max_month AS (
    SELECT date_trunc('month', MAX(order_date))::DATE AS last_month
    FROM core.fct_orders WHERE is_valid_order
)
SELECT
    a.cohort_month,
    a.months_since,
    s.cohort_size,
    a.active_customers,
    a.orders,
    a.net_revenue,
    a.gross_profit,
    a.active_customers::DOUBLE / s.cohort_size          AS retention_rate,
    a.net_revenue / s.cohort_size                       AS revenue_per_cohort_customer,

    -- Number of whole months this cohort has actually been observable for.
    date_diff('month', a.cohort_month, m.last_month)    AS months_observable,
    (a.months_since <= date_diff('month', a.cohort_month, m.last_month)) AS is_mature
FROM activity a
JOIN cohort_sizes s USING (cohort_month)
CROSS JOIN max_month m
ORDER BY a.cohort_month, a.months_since;
