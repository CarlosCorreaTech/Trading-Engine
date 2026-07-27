-- Semantic: cumulative revenue per acquired customer at fixed day offsets.
--
-- READ THE CAVEAT BEFORE USING THIS TABLE.
--
-- This is the conventional way to value a customer, and for cohorts acquired
-- from roughly October 2024 onward it does not work on this dataset. Repeat
-- purchasing in the source data is confined almost entirely to customers
-- acquired in July-September 2024: those three cohorts generate 98% of all
-- repeat orders in 2025, and no customer acquired after 2025-02-19 ever places
-- a second order. That is not a behaviour pattern, it is an artifact of how
-- the synthetic data was produced (see sql/semantic/04_cohort_repeat_windows
-- and src/detection/cohort_adjudication.py for the evidence).
--
-- The table is built anyway, for two reasons. It is the evidence that makes
-- the artifact visible and quantifiable, and on a real dataset it is exactly
-- the model the engine would use.
--
-- What it must not be used for is the CAC:LTV comparison behind the budget
-- recommendation. Feeding a fabricated near-zero LTV into that decision would
-- make every channel look catastrophically unprofitable for reasons that have
-- nothing to do with the brand. semantic.channel_unit_economics uses
-- first-order contribution margin instead, which is measured from data that
-- is intact.

CREATE OR REPLACE TABLE semantic.cohort_ltv_curve AS
WITH bounds AS (
    SELECT MAX(order_date) AS last_date FROM core.fct_orders WHERE is_valid_order
),
offsets AS (
    SELECT * FROM (VALUES (30), (60), (90), (180)) AS t(day_offset)
),
cohort_sizes AS (
    SELECT cohort_month, COUNT(*) AS cohort_size
    FROM core.dim_customer
    WHERE has_purchased
    GROUP BY 1
),
cumulative AS (
    SELECT
        c.cohort_month,
        o.day_offset,
        SUM(f.net_revenue)   AS cumulative_net_revenue,
        SUM(f.gross_profit)  AS cumulative_gross_profit,
        COUNT(*)             AS cumulative_orders
    FROM core.dim_customer c
    JOIN core.fct_orders f
      ON f.customer_id = c.customer_id
     AND f.is_valid_order
    CROSS JOIN offsets o
    WHERE c.has_purchased
      AND date_diff('day', c.first_order_date, f.order_date) BETWEEN 0 AND o.day_offset
    GROUP BY 1, 2
)
SELECT
    cu.cohort_month,
    cu.day_offset,
    s.cohort_size,
    cu.cumulative_orders,
    cu.cumulative_net_revenue,
    cu.cumulative_gross_profit,
    cu.cumulative_net_revenue / s.cohort_size       AS ltv_revenue_per_customer,
    cu.cumulative_gross_profit / s.cohort_size      AS ltv_gross_profit_per_customer,
    cu.cumulative_orders::DOUBLE / s.cohort_size    AS orders_per_customer,

    -- A cohort only has a valid reading at an offset once that many days have
    -- actually elapsed. Without this guard the most recent cohorts appear to
    -- have low LTV purely because the window is still open.
    date_diff('day', cu.cohort_month, b.last_date)  AS days_observable,
    (date_diff('day', cu.cohort_month, b.last_date) >= cu.day_offset) AS is_mature
FROM cumulative cu
JOIN cohort_sizes s USING (cohort_month)
CROSS JOIN bounds b
ORDER BY cu.cohort_month, cu.day_offset;
