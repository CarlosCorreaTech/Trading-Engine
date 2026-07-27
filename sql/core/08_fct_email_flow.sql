-- Core: email flow fact, one row per message per day.
--
-- flow_stage orders the funnel by intent rather than alphabetically, because
-- the flows are not comparable to each other: a Cart Abandonment message goes
-- to someone with a loaded basket and converts at ~4%, while a Win-Back goes
-- to a lapsed customer and converts at ~0.5%. Detecting a change in either
-- requires comparing a flow against its own history, never against another
-- flow, and the stage label makes that grouping explicit.
--
-- The four funnel rates are carried through from staging deliberately. The
-- signal that matters in this dataset is not any single rate falling, but
-- opens and clicks falling while order rate holds; that only becomes visible
-- when the rates sit side by side at a common grain.

CREATE OR REPLACE TABLE core.fct_email_flow AS
SELECT
    e.flow_date,
    e.flow_id,
    e.flow_name,
    e.message_id,
    e.message_name,

    CASE e.flow_name
        WHEN 'Welcome Series'      THEN 'Acquisition'
        WHEN 'Browse Abandonment'  THEN 'Conversion'
        WHEN 'Cart Abandonment'    THEN 'Conversion'
        WHEN 'Post-Purchase'       THEN 'Retention'
        WHEN 'Replenishment'       THEN 'Retention'
        WHEN 'Win-Back'            THEN 'Reactivation'
        ELSE 'Other'
    END                                             AS flow_stage,

    e.recipients,
    e.unique_opens,
    e.unique_clicks,
    e.unique_orders,
    e.unique_unsubscribes,
    e.order_value                                   AS revenue_gross,
    ROUND(e.order_value / (1 + 0.20), 2)            AS revenue_net,

    e.open_rate,
    e.click_rate,
    e.order_rate,
    e.click_to_open_rate,
    e.unsubscribe_rate,
    CASE WHEN e.recipients > 0
         THEN e.order_value / (1 + 0.20) / e.recipients
    END                                             AS revenue_per_recipient,

    e.flow_status,
    e.message_status
FROM staging.stg_email_flows e;
