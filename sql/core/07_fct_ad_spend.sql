-- Core: unified daily ad spend across Meta and Google.
--
-- Unioning the two platforms into one grain (channel x campaign x day) is what
-- lets the engine compare them, but the union has to be honest about what each
-- platform does and does not report:
--
--   Meta supplies spend, impressions, clicks and reach, but no conversions and
--   no revenue. Its conversion columns are NULL here, not zero. Zero would
--   read as "this campaign sold nothing", which is false; NULL correctly says
--   "this platform did not tell us".
--
--   Google supplies platform-attributed conversions and conversion value. Its
--   reach and frequency columns are NULL for the same reason.
--
-- The consequence, which the semantic layer carries forward, is that Meta
-- efficiency can only be measured site-side (spend against attributed orders)
-- while Google can be measured either way. Comparing Google's platform ROAS
-- against Meta's site-attributed ROAS is not like for like, so the semantic
-- layer computes both channels site-side for any head-to-head decision.
--
-- Spend is ex-VAT: advertising costs are billed net and reclaimable, so they
-- are already on the same basis as the ex-VAT revenue in fct_orders.

CREATE OR REPLACE TABLE core.fct_ad_spend AS
SELECT
    ad_date,
    'Meta'                          AS channel,
    campaign_id,
    campaign_name,
    -- Meta campaign names encode intent, which matters because prospecting and
    -- retargeting have structurally different costs and should not share a
    -- baseline.
    CASE
        WHEN campaign_name ILIKE 'Prospecting%' THEN 'Prospecting'
        WHEN campaign_name ILIKE 'Retargeting%' THEN 'Retargeting'
        WHEN campaign_name ILIKE 'DPA%'         THEN 'Retargeting'
        ELSE 'Other'
    END                             AS campaign_intent,
    spend,
    impressions,
    clicks,
    reach,
    frequency,
    CAST(NULL AS DOUBLE)            AS platform_conversions,
    CAST(NULL AS DECIMAL(12, 2))    AS platform_conversion_value
FROM staging.stg_meta_ads_daily

UNION ALL

SELECT
    ad_date,
    'Google'                        AS channel,
    campaign_id,
    campaign_name,
    CASE
        WHEN campaign_name ILIKE 'Brand%'     THEN 'Brand'
        WHEN campaign_name ILIKE 'Non-Brand%' THEN 'Prospecting'
        WHEN campaign_name ILIKE 'Shopping%'  THEN 'Shopping'
        WHEN campaign_name ILIKE 'PMax%'      THEN 'PMax'
        ELSE 'Other'
    END                             AS campaign_intent,
    spend,
    impressions,
    clicks,
    CAST(NULL AS BIGINT)            AS reach,
    CAST(NULL AS DOUBLE)            AS frequency,
    conversions                     AS platform_conversions,
    conversions_value               AS platform_conversion_value
FROM staging.stg_google_ads_daily;
