-- Semantic: daily paid-channel efficiency, and the model the Meta signal rests on.
--
-- The hard problem here is the CAC denominator. 27% of orders carry no usable
-- referrer, so any single CAC number is a choice dressed up as a fact. Two
-- bounds are produced instead:
--
--   cac_attributed      spend / new customers this channel provably won.
--                       Pessimistic: credits the channel with nothing it
--                       cannot prove, so CAC looks as expensive as possible.
--
--   cac_with_unattributed
--                       spend / (attributed + a pro-rata slice of the
--                       unattributed pool). Optimistic: assumes lost referrers
--                       are distributed like known ones.
--
-- The truth sits between them. A recommendation that only holds at one end of
-- the range is not safe to act on, and the engine checks exactly that before
-- recommending a budget move.
--
-- The unattributed pool is shared across every channel that won customers,
-- including TikTok. Splitting it between Meta and Google alone would quietly
-- hand TikTok's share to the two channels being compared and flatter both.
--
-- Meta reports no conversions, so both channels are measured site-side here.
-- Google's platform-reported figures are carried alongside but never mixed
-- into the same comparison, because platform-attributed and site-attributed
-- conversions count different things.

CREATE OR REPLACE TABLE semantic.channel_daily_performance AS
WITH spend_daily AS (
    SELECT
        ad_date                                     AS date_day,
        channel,
        SUM(spend)                                  AS spend,
        SUM(clicks)                                 AS clicks,
        SUM(impressions)                            AS impressions,
        SUM(platform_conversions)                   AS platform_conversions,
        SUM(platform_conversion_value)              AS platform_conversion_value,
        COUNT(*)                                    AS campaigns_reporting
    FROM core.fct_ad_spend
    GROUP BY 1, 2
),
orders_daily AS (
    SELECT
        order_date                                  AS date_day,
        channel,
        COUNT(*)                                    AS orders,
        COUNT(*) FILTER (WHERE is_first_order)      AS new_customers,
        SUM(net_revenue)                            AS net_revenue,
        SUM(gross_profit)                           AS gross_profit,
        SUM(net_revenue) FILTER (WHERE is_first_order) AS new_customer_revenue
    FROM core.fct_orders
    WHERE is_valid_order
    GROUP BY 1, 2
),
-- The pool of first orders we could not attribute to any channel.
unattributed_daily AS (
    SELECT date_day, SUM(new_customers) AS unattributed_new_customers
    FROM orders_daily
    WHERE channel IN ('Direct', 'Unknown')
    GROUP BY 1
),
-- Total attributed first orders, used as the pro-rata basis.
attributed_daily AS (
    SELECT date_day, SUM(new_customers) AS attributed_new_customers
    FROM orders_daily
    WHERE channel NOT IN ('Direct', 'Unknown')
    GROUP BY 1
),
combined AS (
    SELECT
        COALESCE(s.date_day, o.date_day)            AS date_day,
        COALESCE(s.channel, o.channel)              AS channel,
        s.spend,
        s.clicks,
        s.impressions,
        s.platform_conversions,
        s.platform_conversion_value,
        s.campaigns_reporting,
        COALESCE(o.orders, 0)                       AS orders,
        COALESCE(o.new_customers, 0)                AS new_customers,
        COALESCE(o.net_revenue, 0)                  AS net_revenue,
        COALESCE(o.gross_profit, 0)                 AS gross_profit,
        COALESCE(o.new_customer_revenue, 0)         AS new_customer_revenue,
        u.unattributed_new_customers,
        a.attributed_new_customers
    FROM spend_daily s
    FULL OUTER JOIN orders_daily o
        ON o.date_day = s.date_day AND o.channel = s.channel
    LEFT JOIN unattributed_daily u ON u.date_day = COALESCE(s.date_day, o.date_day)
    LEFT JOIN attributed_daily  a ON a.date_day = COALESCE(s.date_day, o.date_day)
),
allocated AS (
    SELECT
        *,
        CASE
            WHEN channel IN ('Direct', 'Unknown') THEN 0
            WHEN COALESCE(attributed_new_customers, 0) = 0 THEN 0
            ELSE COALESCE(unattributed_new_customers, 0)
                 * (new_customers::DOUBLE / attributed_new_customers)
        END                                         AS allocated_unattributed
    FROM combined
),
metrics AS (
    SELECT
        date_day,
        channel,
        spend,
        clicks,
        impressions,
        campaigns_reporting,
        platform_conversions,
        platform_conversion_value,
        orders,
        new_customers,
        net_revenue,
        gross_profit,
        new_customer_revenue,
        allocated_unattributed,
        new_customers + allocated_unattributed      AS new_customers_upper,

        CASE WHEN clicks > 0 THEN spend / clicks END                    AS cpc,
        CASE WHEN impressions > 0 THEN clicks::DOUBLE / impressions END AS ctr,
        CASE WHEN impressions > 0 THEN spend / impressions * 1000 END   AS cpm,

        -- Pessimistic and optimistic bounds; see header note.
        CASE WHEN new_customers > 0 THEN spend / new_customers END      AS cac_attributed,
        CASE WHEN new_customers + allocated_unattributed > 0
             THEN spend / (new_customers + allocated_unattributed) END  AS cac_with_unattributed,

        CASE WHEN spend > 0 THEN net_revenue / spend END                AS roas_site,
        CASE WHEN spend > 0 THEN gross_profit / spend END               AS profit_roas_site,
        CASE WHEN spend > 0 AND platform_conversion_value IS NOT NULL
             THEN platform_conversion_value / spend END                 AS roas_platform,
        CASE WHEN clicks > 0 THEN orders::DOUBLE / clicks END           AS site_conversion_rate
    FROM allocated
)
SELECT
    *,
    -- Daily CAC on a few hundred pounds of spend is extremely noisy; the
    -- 7- and 28-day views are what the detectors actually read.
    AVG(cpc) OVER w7             AS cpc_ma7,
    AVG(cpc) OVER w28            AS cpc_ma28,
    SUM(spend) OVER w7           AS spend_7d,
    SUM(new_customers) OVER w7   AS new_customers_7d,
    CASE WHEN SUM(new_customers) OVER w7 > 0
         THEN SUM(spend) OVER w7 / SUM(new_customers) OVER w7 END       AS cac_attributed_7d,
    CASE WHEN SUM(new_customers_upper) OVER w7 > 0
         THEN SUM(spend) OVER w7 / SUM(new_customers_upper) OVER w7 END AS cac_with_unattributed_7d,
    CASE WHEN SUM(spend) OVER w7 > 0
         THEN SUM(net_revenue) OVER w7 / SUM(spend) OVER w7 END         AS roas_site_7d
FROM metrics
WINDOW
    w7  AS (PARTITION BY channel ORDER BY date_day RANGE BETWEEN INTERVAL 6 DAY PRECEDING AND CURRENT ROW),
    w28 AS (PARTITION BY channel ORDER BY date_day RANGE BETWEEN INTERVAL 27 DAY PRECEDING AND CURRENT ROW)
ORDER BY channel, date_day;
