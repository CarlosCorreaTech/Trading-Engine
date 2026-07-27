-- Semantic: one row per calendar day, the brand's top-line trading position.
--
-- Built off dim_date with a LEFT JOIN so every day exists even if nothing
-- happened, which keeps rolling windows honest.
--
-- The AOV decomposition is the point of this model. Headline AOV falls from
-- about 61.44 to 55.01 across the year, and there are only three ways that can
-- happen: customers buy fewer items, they get bigger discounts, or the mix
-- shifts toward cheaper products. Carrying units_per_order, discount_rate and
-- avg_unit_price alongside AOV means the detector can tell which, instead of
-- firing a generic "AOV is falling" alert that invites the wrong fix.
--
-- Rolling averages use RANGE over the date, not ROWS, so a missing day widens
-- no window and silently drops out rather than pulling an older day in.

CREATE OR REPLACE TABLE semantic.daily_business_metrics AS
WITH order_daily AS (
    SELECT
        order_date,
        COUNT(*)                                        AS orders,
        COUNT(*) FILTER (WHERE is_first_order)          AS new_customers,
        COUNT(*) FILTER (WHERE NOT is_first_order)      AS repeat_orders,
        SUM(net_revenue)                                AS net_revenue,
        SUM(gross_profit)                               AS gross_profit,
        SUM(total_cogs)                                 AS cogs,
        SUM(discounts)                                  AS discounts,
        SUM(line_items_price)                           AS gross_merchandise_value,
        SUM(total_units)                                AS units,
        SUM(shipping_price)                             AS shipping_revenue
    FROM core.fct_orders
    WHERE is_valid_order
    GROUP BY 1
),
cancel_daily AS (
    SELECT
        order_date,
        COUNT(*)                                        AS cancelled_orders,
        COUNT(*) FILTER (WHERE is_fraud)                AS fraud_orders,
        SUM(net_revenue)                                AS cancelled_revenue
    FROM core.fct_orders
    WHERE is_cancelled
    GROUP BY 1
),
joined AS (
    SELECT
        d.date_day,
        d.year_month,
        d.week_start,
        d.day_of_week,
        d.day_name,
        d.is_weekend,
        d.is_peak_season,
        d.day_index,

        COALESCE(o.orders, 0)                           AS orders,
        COALESCE(o.new_customers, 0)                    AS new_customers,
        COALESCE(o.repeat_orders, 0)                    AS repeat_orders,
        COALESCE(o.net_revenue, 0)                      AS net_revenue,
        COALESCE(o.gross_profit, 0)                     AS gross_profit,
        COALESCE(o.cogs, 0)                             AS cogs,
        COALESCE(o.discounts, 0)                        AS discounts,
        COALESCE(o.gross_merchandise_value, 0)          AS gross_merchandise_value,
        COALESCE(o.units, 0)                            AS units,
        COALESCE(c.cancelled_orders, 0)                 AS cancelled_orders,
        COALESCE(c.fraud_orders, 0)                     AS fraud_orders,

        -- AOV and its three drivers, so a movement can be attributed rather
        -- than merely observed.
        CASE WHEN o.orders > 0 THEN o.net_revenue / o.orders END        AS aov,
        CASE WHEN o.orders > 0 THEN o.units::DOUBLE / o.orders END      AS units_per_order,
        CASE WHEN o.units > 0 THEN o.net_revenue / o.units END          AS avg_unit_price,
        CASE WHEN o.gross_merchandise_value > 0
             THEN o.discounts / o.gross_merchandise_value END           AS discount_rate,

        CASE WHEN o.net_revenue > 0 THEN o.gross_profit / o.net_revenue END AS gross_margin_pct,
        CASE WHEN o.orders > 0 THEN o.repeat_orders::DOUBLE / o.orders END  AS repeat_order_share,
        CASE WHEN COALESCE(o.orders, 0) + COALESCE(c.cancelled_orders, 0) > 0
             THEN c.cancelled_orders::DOUBLE / (o.orders + c.cancelled_orders) END AS cancellation_rate
    FROM core.dim_date d
    LEFT JOIN order_daily o  ON o.order_date = d.date_day
    LEFT JOIN cancel_daily c ON c.order_date = d.date_day
)
SELECT
    *,
    AVG(net_revenue) OVER w7   AS net_revenue_ma7,
    AVG(net_revenue) OVER w28  AS net_revenue_ma28,
    AVG(orders) OVER w7        AS orders_ma7,
    AVG(aov) OVER w7           AS aov_ma7,
    AVG(aov) OVER w28          AS aov_ma28,
    AVG(new_customers) OVER w7 AS new_customers_ma7
FROM joined
WINDOW
    w7  AS (ORDER BY date_day RANGE BETWEEN INTERVAL 6 DAY PRECEDING AND CURRENT ROW),
    w28 AS (ORDER BY date_day RANGE BETWEEN INTERVAL 27 DAY PRECEDING AND CURRENT ROW)
ORDER BY date_day;
