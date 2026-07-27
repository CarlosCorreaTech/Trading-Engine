-- Staging: Meta Ads daily campaign performance (one row per campaign per day).
--
-- Critically, Meta reports no conversions or revenue in this extract, only
-- spend and traffic. Google reports its own conversions and conversion value.
-- The two channels are therefore measured asymmetrically, and any Meta CAC has
-- to be built from site-side attribution (orders.referring_site) rather than
-- platform-reported conversions. The semantic layer makes that explicit
-- instead of quietly comparing platform-reported Google numbers against
-- site-attributed Meta numbers, which would not be like for like.
--
-- cpc, cpm and ctr arrive pre-computed. They are kept as *_reported and
-- recomputed from the raw counts alongside, so the DQ layer can confirm the
-- platform's own arithmetic before any detector trusts a CPC trend.
--
-- This feed has a genuine hole: 2025-03-15 and 2025-03-16 are missing for all
-- six campaigns (363 days present, not 365), which is roughly 793 GBP of
-- unrecorded spend. It is left absent rather than zero-filled, because a zero
-- would read as "we paused advertising" and drag any CAC average for March
-- downwards. The DQ layer asserts the gap and downstream metrics use daily
-- averages over observed days rather than assuming a complete calendar.

CREATE OR REPLACE TABLE staging.stg_meta_ads_daily AS
SELECT
    campaign_id,
    campaign_name,
    account_id,
    account_name,
    account_currency                                AS currency,
    CAST(date AS DATE)                              AS ad_date,

    CAST(impressions AS BIGINT)                     AS impressions,
    CAST(clicks AS BIGINT)                          AS clicks,
    CAST(spend AS DECIMAL(12, 2))                   AS spend,
    CAST(reach AS BIGINT)                           AS reach,
    CAST(frequency AS DOUBLE)                       AS frequency,

    CAST(cpc AS DOUBLE)                             AS cpc_reported,
    CAST(cpm AS DOUBLE)                             AS cpm_reported,
    CAST(ctr AS DOUBLE)                             AS ctr_reported,

    CASE WHEN clicks > 0 THEN spend / clicks END                        AS cpc_calc,
    CASE WHEN impressions > 0 THEN spend / impressions * 1000 END       AS cpm_calc,
    CASE WHEN impressions > 0 THEN clicks::DOUBLE / impressions * 100 END AS ctr_calc,

    CAST(_weld_synced AS TIMESTAMP)                 AS synced_at
FROM read_csv(
    getvariable('data_dir') || '/meta_ads_daily.csv',
    header = true,
    timestampformat = '%Y-%m-%dT%H:%M:%S'
);
