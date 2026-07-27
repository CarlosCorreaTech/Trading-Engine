-- Staging: Klaviyo automated flow performance (one row per message per day).
--
-- Klaviyo exports use PascalCase headers; they are snake_cased here so the
-- warehouse has one naming convention throughout.
--
-- The engagement funnel is recipients -> opens -> clicks -> orders. Keeping
-- all four rates side by side is what lets the divergence detector work: when
-- opens and clicks fall but placed-order rate holds, the drop is in the
-- measurement of engagement, not in the commercial outcome. That distinction
-- is only visible if the rates are modelled together at a common grain.
--
-- Rates are stored as fractions, not percentages, so every ratio in the
-- warehouse is on the same scale.

CREATE OR REPLACE TABLE staging.stg_email_flows AS
SELECT
    CAST(Run_Date AS DATE)                          AS flow_date,
    Account                                         AS account_name,
    Currency                                        AS currency,

    Flow_ID                                         AS flow_id,
    Flow_Name                                       AS flow_name,
    Message_ID                                      AS message_id,
    Message_Name                                    AS message_name,
    Message_Channel                                 AS message_channel,
    Status                                          AS flow_status,
    Message_Status                                  AS message_status,
    Tags                                            AS tags,

    CAST(Total_Recipients AS BIGINT)                AS recipients,
    CAST(Unique_Opens AS BIGINT)                    AS unique_opens,
    CAST(Unique_Clicks AS BIGINT)                   AS unique_clicks,
    CAST(Unique_Unsubscribes AS BIGINT)             AS unique_unsubscribes,
    CAST(Unique_Placed_Order AS BIGINT)             AS unique_orders,
    CAST(Total_Placed_Order AS BIGINT)              AS total_orders,
    CAST(Total_Placed_Order_Value AS DECIMAL(12, 2)) AS order_value,

    CASE WHEN Total_Recipients > 0 THEN Unique_Opens::DOUBLE / Total_Recipients END          AS open_rate,
    CASE WHEN Total_Recipients > 0 THEN Unique_Clicks::DOUBLE / Total_Recipients END         AS click_rate,
    CASE WHEN Total_Recipients > 0 THEN Unique_Placed_Order::DOUBLE / Total_Recipients END   AS order_rate,
    CASE WHEN Total_Recipients > 0 THEN Unique_Unsubscribes::DOUBLE / Total_Recipients END   AS unsubscribe_rate,
    -- Click-to-open isolates "did the people who saw it engage" from "did it
    -- get delivered and rendered", which helps separate deliverability from
    -- creative fatigue.
    CASE WHEN Unique_Opens > 0 THEN Unique_Clicks::DOUBLE / Unique_Opens END                 AS click_to_open_rate,
    CASE WHEN Unique_Placed_Order > 0 THEN Total_Placed_Order_Value / Unique_Placed_Order END AS revenue_per_order,

    CAST(_weld_synced AS TIMESTAMP)                 AS synced_at
FROM read_csv(
    getvariable('data_dir') || '/email_flows.csv',
    header = true,
    timestampformat = '%Y-%m-%dT%H:%M:%S'
);
