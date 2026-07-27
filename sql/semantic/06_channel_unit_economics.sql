-- Semantic: monthly unit economics per paid channel. This is the decision
-- table behind the budget recommendation.
--
-- The usual test is CAC against LTV. That test is unavailable here: repeat
-- purchasing in this dataset is a generation artifact (see
-- 04_cohort_repeat_windows), so any LTV for a recent cohort is fiction, and
-- fiction in the denominator would condemn every channel equally.
--
-- The engine falls back to the strictest defensible test instead: does the
-- first order alone cover the cost of acquiring the customer?
--
--   contribution_per_new_customer   gross profit on the customer's first order
--   payback_ratio                   that profit divided by CAC
--
-- A payback_ratio above 1.0 means the channel is self-funding from the first
-- purchase, before any repeat revenue. Below 1.0 means the brand is buying
-- customers at a loss and needs repeat purchasing to bail it out, which on
-- this dataset cannot be verified.
--
-- This is deliberately conservative. Real brands routinely and sensibly run
-- first-order payback below 1.0 because they know their repeat rate. Saying so
-- explicitly matters: the recommendation this feeds is framed as "Meta has
-- crossed a line Google has not", a relative judgement between two channels
-- measured identically, rather than an absolute claim that Meta is
-- unprofitable. The relative comparison holds regardless of the LTV problem.
--
-- Both CAC bounds are carried through so a recommendation can be tested
-- against the pessimistic and optimistic attribution assumptions.

CREATE OR REPLACE TABLE semantic.channel_unit_economics AS
WITH monthly AS (
    SELECT
        date_trunc('month', date_day)::DATE         AS month_start,
        channel,
        SUM(spend)                                  AS spend,
        SUM(clicks)                                 AS clicks,
        SUM(impressions)                            AS impressions,
        SUM(orders)                                 AS orders,
        SUM(new_customers)                          AS new_customers,
        SUM(allocated_unattributed)                 AS allocated_unattributed,
        SUM(net_revenue)                            AS net_revenue,
        SUM(gross_profit)                           AS gross_profit,
        COUNT(DISTINCT date_day) FILTER (WHERE spend IS NOT NULL) AS days_with_spend
    FROM semantic.channel_daily_performance
    GROUP BY 1, 2
),
first_order_economics AS (
    -- Gross profit earned on first orders only, per channel per month. This is
    -- what a newly acquired customer is actually worth on day one.
    SELECT
        date_trunc('month', order_date)::DATE       AS month_start,
        channel,
        COUNT(*)                                    AS first_orders,
        SUM(gross_profit)                           AS first_order_gross_profit,
        AVG(gross_profit)                           AS avg_first_order_gross_profit,
        AVG(net_revenue)                            AS avg_first_order_net_revenue
    FROM core.fct_orders
    WHERE is_valid_order AND is_first_order
    GROUP BY 1, 2
)
SELECT
    m.month_start,
    m.channel,
    m.days_with_spend,
    m.spend,
    m.clicks,
    m.impressions,
    m.orders,
    m.new_customers,
    m.allocated_unattributed,
    m.new_customers + m.allocated_unattributed      AS new_customers_upper,
    m.net_revenue,
    m.gross_profit,

    CASE WHEN m.clicks > 0 THEN m.spend / m.clicks END          AS cpc,
    CASE WHEN m.impressions > 0 THEN m.clicks::DOUBLE / m.impressions END AS ctr,

    CASE WHEN m.new_customers > 0 THEN m.spend / m.new_customers END AS cac_attributed,
    CASE WHEN m.new_customers + m.allocated_unattributed > 0
         THEN m.spend / (m.new_customers + m.allocated_unattributed) END AS cac_with_unattributed,

    f.avg_first_order_net_revenue,
    f.avg_first_order_gross_profit                  AS contribution_per_new_customer,

    -- Payback under both attribution assumptions. If the pessimistic reading
    -- is below 1.0 but the optimistic one is above, the channel is borderline
    -- and the engine should say so rather than pick the flattering number.
    CASE WHEN m.new_customers > 0 AND m.spend > 0
         THEN f.avg_first_order_gross_profit / (m.spend / m.new_customers) END AS payback_ratio_pessimistic,
    CASE WHEN m.new_customers + m.allocated_unattributed > 0 AND m.spend > 0
         THEN f.avg_first_order_gross_profit
              / (m.spend / (m.new_customers + m.allocated_unattributed)) END   AS payback_ratio_optimistic,

    CASE WHEN m.spend > 0 THEN m.net_revenue / m.spend END      AS roas_site,
    CASE WHEN m.spend > 0 THEN m.gross_profit / m.spend END     AS profit_roas_site
FROM monthly m
LEFT JOIN first_order_economics f
       ON f.month_start = m.month_start AND f.channel = m.channel
WHERE m.spend IS NOT NULL
ORDER BY m.channel, m.month_start;
