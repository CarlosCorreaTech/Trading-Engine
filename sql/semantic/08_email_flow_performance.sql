-- Semantic: email flow performance with the full funnel side by side.
--
-- Grain is flow x day, aggregated up from individual messages, because a flow
-- is the unit an operator would actually change.
--
-- This model is built to answer one question: when engagement falls, did
-- customer behaviour change or did measurement change? Those demand opposite
-- responses, and telling them apart requires the whole funnel at once:
--
--   open_rate    most fragile. Depends on a tracking pixel loading, so it
--                breaks for reasons that have nothing to do with the customer.
--   click_rate   more robust, but still a tracked redirect.
--   order_rate   the commercial outcome, measured by Shopify rather than the
--                email platform, so it survives an email tracking failure.
--
-- If opens and clicks fall together while order rate holds, the emails are
-- still working and the measurement is not. If order rate falls with them,
-- the audience really has disengaged. The engine keys on that comparison
-- rather than alerting on open rate alone, which is the mistake that leads to
-- rewriting flows that were never broken.
--
-- Rates are recomputed from summed numerators and denominators rather than
-- averaged from the message-level rates, since averaging ratios weights a
-- message sent to 20 people the same as one sent to 2,000.

CREATE OR REPLACE TABLE semantic.email_flow_performance AS
WITH flow_daily AS (
    SELECT
        flow_date                                   AS date_day,
        flow_name,
        flow_stage,
        COUNT(DISTINCT message_id)                  AS messages,
        SUM(recipients)                             AS recipients,
        SUM(unique_opens)                           AS opens,
        SUM(unique_clicks)                          AS clicks,
        SUM(unique_orders)                          AS orders,
        SUM(unique_unsubscribes)                    AS unsubscribes,
        SUM(revenue_net)                            AS revenue_net
    FROM core.fct_email_flow
    GROUP BY 1, 2, 3
),
rated AS (
    SELECT
        *,
        CASE WHEN recipients > 0 THEN opens::DOUBLE / recipients END        AS open_rate,
        CASE WHEN recipients > 0 THEN clicks::DOUBLE / recipients END       AS click_rate,
        CASE WHEN recipients > 0 THEN orders::DOUBLE / recipients END       AS order_rate,
        CASE WHEN recipients > 0 THEN unsubscribes::DOUBLE / recipients END AS unsubscribe_rate,
        CASE WHEN opens > 0 THEN clicks::DOUBLE / opens END                 AS click_to_open_rate,
        CASE WHEN clicks > 0 THEN orders::DOUBLE / clicks END               AS click_to_order_rate,
        CASE WHEN recipients > 0 THEN revenue_net / recipients END          AS revenue_per_recipient
    FROM flow_daily
)
SELECT
    *,
    -- Flows do not send every day, so windows are date-ranged and the counts
    -- are summed before the ratio is taken.
    SUM(recipients) OVER w28    AS recipients_28d,
    SUM(opens) OVER w28         AS opens_28d,
    SUM(clicks) OVER w28        AS clicks_28d,
    SUM(orders) OVER w28        AS orders_28d,

    CASE WHEN SUM(recipients) OVER w28 > 0
         THEN SUM(opens) OVER w28::DOUBLE / SUM(recipients) OVER w28 END    AS open_rate_28d,
    CASE WHEN SUM(recipients) OVER w28 > 0
         THEN SUM(clicks) OVER w28::DOUBLE / SUM(recipients) OVER w28 END   AS click_rate_28d,
    CASE WHEN SUM(recipients) OVER w28 > 0
         THEN SUM(orders) OVER w28::DOUBLE / SUM(recipients) OVER w28 END   AS order_rate_28d,
    CASE WHEN SUM(recipients) OVER w28 > 0
         THEN SUM(revenue_net) OVER w28 / SUM(recipients) OVER w28 END      AS revenue_per_recipient_28d
FROM rated
WINDOW
    w28 AS (PARTITION BY flow_name ORDER BY date_day RANGE BETWEEN INTERVAL 27 DAY PRECEDING AND CURRENT ROW)
ORDER BY flow_name, date_day;
