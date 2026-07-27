-- Staging: Google Ads daily campaign performance (one row per campaign per day).
--
-- cost_micros is the Google Ads API convention: currency units x 1,000,000.
-- Converted to pounds once, here, so no downstream model can forget the
-- divisor and report a CPA six orders of magnitude too large.
--
-- device and ad_network_type look like dimensions but are not: there is
-- exactly one row per campaign per day, so each carries a single arbitrary
-- value rather than a breakdown. They are retained for lineage and explicitly
-- not used for grouping.
--
-- conversions is fractional (Google's attribution model splits credit across
-- touchpoints), which is why it is a DOUBLE rather than an integer count, and
-- why platform-reported conversions will not tie to a count of Shopify orders.

CREATE OR REPLACE TABLE staging.stg_google_ads_daily AS
SELECT
    campaign_id,
    campaign_name,
    CAST(account_id AS BIGINT)                      AS account_id,
    account_descriptive_name                        AS account_name,
    CAST(date AS DATE)                              AS ad_date,

    CAST(impressions AS BIGINT)                     AS impressions,
    CAST(clicks AS BIGINT)                          AS clicks,
    CAST(cost_micros AS BIGINT)                     AS cost_micros,
    CAST(cost_micros / 1000000.0 AS DECIMAL(12, 2)) AS spend,

    CAST(conversions AS DOUBLE)                     AS conversions,
    CAST(conversions_value AS DECIMAL(12, 2))       AS conversions_value,

    CASE WHEN clicks > 0 THEN cost_micros / 1000000.0 / clicks END           AS cpc_calc,
    CASE WHEN conversions > 0 THEN cost_micros / 1000000.0 / conversions END AS cpa_calc,
    CASE WHEN cost_micros > 0 THEN conversions_value / (cost_micros / 1000000.0) END AS roas_calc,

    device                                          AS device_raw,
    ad_network_type                                 AS ad_network_type_raw,

    CAST(_weld_synced AS TIMESTAMP)                 AS synced_at
FROM read_csv(
    getvariable('data_dir') || '/google_ads_daily.csv',
    header = true,
    timestampformat = '%Y-%m-%dT%H:%M:%S'
);
