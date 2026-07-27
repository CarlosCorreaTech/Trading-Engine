-- Semantic: fixed-window repeat rates per cohort, built to adjudicate one
-- specific question.
--
-- The raw data shows 90-day repeat rate falling from 32% for the July 2024
-- cohort to effectively 0% from December 2024 onward. Taken at face value that
-- is a catastrophic retention collapse and the single biggest finding in the
-- dataset. But the brand's overall repeat-order share is flat at ~24% across
-- the same period, and both cannot be true: if no new customer ever returns,
-- the repeat share has to fall as older cohorts exhaust.
--
-- So one of the two is an artifact, and this table exists to find out which.
-- It carries the evidence for a specific competing hypothesis: that repeat
-- purchasers were drawn roughly uniformly from the whole customer base, in
-- which case each individual's chance of repeating within a fixed window falls
-- as the base grows, purely mechanically and with no behavioural change at
-- all. The customer base grows from 1,879 to 20,817 over the year, so that
-- mechanism predicts an order-of-magnitude decay on its own.
--
-- The columns needed to test that are:
--   pool_size_at_cohort_start   customers already acquired when the cohort landed
--   market_repeat_orders_*      brand-wide repeat orders during the window
-- If observed repeat rate tracks market_repeat_orders / pool_size, the decay
-- is mechanical. If it falls faster, something real is happening.
--
-- has_full_* flags guard against the other trap: recent cohorts have not had
-- 90 days to come back, so including them would manufacture a decline
-- regardless. Only cohorts with a complete window are comparable.

CREATE OR REPLACE TABLE semantic.cohort_repeat_windows AS
WITH bounds AS (
    SELECT MAX(order_date) AS last_date FROM core.fct_orders WHERE is_valid_order
),
customer_first AS (
    SELECT customer_id, first_order_date, cohort_month
    FROM core.dim_customer
    WHERE has_purchased
),
repeat_flags AS (
    SELECT
        f.customer_id,
        f.cohort_month,
        f.first_order_date,
        MAX(CASE WHEN date_diff('day', f.first_order_date, o.order_date) BETWEEN 1 AND 30  THEN 1 ELSE 0 END) AS repeated_30,
        MAX(CASE WHEN date_diff('day', f.first_order_date, o.order_date) BETWEEN 1 AND 60  THEN 1 ELSE 0 END) AS repeated_60,
        MAX(CASE WHEN date_diff('day', f.first_order_date, o.order_date) BETWEEN 1 AND 90  THEN 1 ELSE 0 END) AS repeated_90
    FROM customer_first f
    LEFT JOIN core.fct_orders o
           ON o.customer_id = f.customer_id
          AND o.is_valid_order
          AND o.order_date > f.first_order_date
    GROUP BY 1, 2, 3
),
cohort_agg AS (
    SELECT
        cohort_month,
        COUNT(*)                    AS cohort_size,
        SUM(repeated_30)            AS repeated_within_30d,
        SUM(repeated_60)            AS repeated_within_60d,
        SUM(repeated_90)            AS repeated_within_90d,
        MIN(first_order_date)       AS cohort_start
    FROM repeat_flags
    GROUP BY 1
),
-- How many customers already existed when each cohort landed. This is the
-- denominator of the mechanical-decay hypothesis.
pool AS (
    SELECT
        c.cohort_month,
        COUNT(*) FILTER (WHERE p.first_order_date < c.cohort_month) AS pool_size_at_cohort_start
    FROM (SELECT DISTINCT cohort_month FROM customer_first) c
    CROSS JOIN customer_first p
    GROUP BY 1
),
-- Brand-wide repeat orders in the 90 days following each cohort's start. This
-- is the numerator: the pool of repeat purchases available to be won.
market AS (
    SELECT
        c.cohort_month,
        COUNT(*) FILTER (
            WHERE o.order_date >= c.cohort_month
              AND o.order_date < c.cohort_month + INTERVAL 90 DAY
        ) AS market_repeat_orders_90d
    FROM (SELECT DISTINCT cohort_month FROM customer_first) c
    CROSS JOIN core.fct_orders o
    WHERE o.is_valid_order AND NOT o.is_first_order
    GROUP BY 1
)
SELECT
    a.cohort_month,
    a.cohort_size,
    a.repeated_within_30d,
    a.repeated_within_60d,
    a.repeated_within_90d,
    a.repeated_within_30d::DOUBLE / a.cohort_size    AS repeat_rate_30d,
    a.repeated_within_60d::DOUBLE / a.cohort_size    AS repeat_rate_60d,
    a.repeated_within_90d::DOUBLE / a.cohort_size    AS repeat_rate_90d,

    p.pool_size_at_cohort_start,
    m.market_repeat_orders_90d,
    -- Expected repeat rate if repeat buyers are drawn uniformly from the
    -- existing base. Compared against repeat_rate_90d, this is the test.
    CASE WHEN p.pool_size_at_cohort_start > 0
         THEN m.market_repeat_orders_90d::DOUBLE / p.pool_size_at_cohort_start
    END                                              AS expected_repeat_rate_90d_if_uniform,

    date_diff('day', a.cohort_month, b.last_date)    AS observation_days_available,
    (date_diff('day', a.cohort_month, b.last_date) >= 30) AS has_full_30d_window,
    (date_diff('day', a.cohort_month, b.last_date) >= 60) AS has_full_60d_window,
    (date_diff('day', a.cohort_month, b.last_date) >= 90) AS has_full_90d_window
FROM cohort_agg a
JOIN pool p   USING (cohort_month)
JOIN market m USING (cohort_month)
CROSS JOIN bounds b
ORDER BY a.cohort_month;
