-- Staging: orders (Shopify order headers, one row per order).
--
-- Staging stays faithful to the source. It casts types, normalises empty
-- strings to NULL, derives a referrer domain, and attaches data-quality flags.
-- It does not filter anything out: excluding rows here would hide problems
-- from the DQ layer, so cancelled and suspect orders are flagged and passed on
-- for the core layer to decide about.
--
-- Verified against the raw file:
--   total_price = total_line_items_price - total_discounts + shipping  (26,553/26,553)
--   subtotal_price = total_line_items_price - total_discounts          (26,553/26,553)
-- so the header arithmetic is internally consistent and needs no repair.

CREATE OR REPLACE TABLE staging.stg_orders AS
WITH raw AS (
    SELECT *
    FROM read_csv(
        getvariable('data_dir') || '/orders.csv',
        header = true,
        timestampformat = '%Y-%m-%dT%H:%M:%S'
    )
)
SELECT
    CAST(id AS BIGINT)                                          AS order_id,
    name                                                        AS order_name,
    CAST(order_number AS BIGINT)                                AS order_number,
    CAST(customer_id AS BIGINT)                                 AS customer_id,

    -- Deliberately named _masked. In this extract orders.csv carries a
    -- redacted @mail.com address while customers.csv holds the real domain,
    -- so the two disagree on 22,810 of 26,553 orders. Email is therefore not
    -- a valid join key anywhere in this project; customer_id is. The column is
    -- kept only so the DQ layer can assert the mismatch rather than someone
    -- rediscovering it the hard way.
    lower(nullif(trim(email), ''))                              AS email_masked,

    currency,
    CAST(taxes_included AS BOOLEAN)                             AS taxes_included,

    CAST(total_price AS DECIMAL(12, 2))                         AS total_price_gross,
    CAST(subtotal_price AS DECIMAL(12, 2))                      AS subtotal_price,
    CAST(total_line_items_price AS DECIMAL(12, 2))              AS line_items_price,
    CAST(total_discounts AS DECIMAL(12, 2))                     AS total_discounts,
    CAST(total_tax AS DECIMAL(12, 2))                           AS total_tax,
    CAST(total_shipping_price_set_shop_money_amount AS DECIMAL(12, 2)) AS shipping_price,
    CAST(total_weight AS INTEGER)                               AS total_weight_grams,

    financial_status,
    nullif(trim(fulfillment_status), '')                        AS fulfillment_status,

    CAST(created_at AS TIMESTAMP)                               AS created_at,
    CAST(created_at AS DATE)                                    AS order_date,
    CAST(processed_at AS TIMESTAMP)                             AS processed_at,
    CAST(updated_at AS TIMESTAMP)                               AS updated_at,
    CAST(cancelled_at AS TIMESTAMP)                             AS cancelled_at,
    nullif(trim(cancel_reason), '')                             AS cancel_reason,

    CAST(confirmed AS BOOLEAN)                                  AS is_confirmed,
    CAST(test AS BOOLEAN)                                       AS is_test,
    CAST(buyer_accepts_marketing AS BOOLEAN)                    AS buyer_accepts_marketing,

    source_name,
    nullif(trim(referring_site), '')                            AS referring_site,
    landing_site,

    -- Bare hostname, lowercased and stripped of any www prefix, so the channel
    -- mapping in core matches on a single clean key. 'direct' arrives as a
    -- literal token rather than a URL, and a genuinely absent referrer is NULL;
    -- both are treated as unattributed downstream but kept distinct here
    -- because they mean different things (no referrer vs self-reported direct).
    CASE
        WHEN referring_site IS NULL OR trim(referring_site) = '' THEN NULL
        WHEN lower(trim(referring_site)) = 'direct' THEN 'direct'
        ELSE regexp_replace(
                 regexp_extract(lower(trim(referring_site)), '^https?://([^/]+)', 1),
                 '^www\.', ''
             )
    END                                                         AS referrer_domain,

    shipping_address_country                                    AS country,
    shipping_address_country_code                               AS country_code,
    shipping_address_city                                       AS city,
    customer_locale,
    presentment_currency,

    -- Data-quality flags, resolved once here so every downstream model applies
    -- the same definition of "countable order".
    (cancelled_at IS NOT NULL)                                  AS is_cancelled,
    (lower(financial_status) = 'refunded')                      AS is_refunded,
    (lower(coalesce(cancel_reason, '')) = 'fraud')              AS is_fraud,

    CAST(_weld_synced AS TIMESTAMP)                             AS synced_at
FROM raw;
