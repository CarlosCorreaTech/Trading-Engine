-- Staging: customer profiles and marketing consent.
--
-- Two source columns are pre-aggregated by Shopify and disagree with each
-- other, so both are suffixed _raw and neither is used downstream:
--
--   total_spent   reconciles exactly to the sum of non-cancelled orders.
--   order_count   counts cancelled orders too, so 825 customers disagree
--                 with a recomputed count.
--
-- Rather than pick a winner, core recomputes both from the order facts and the
-- DQ layer asserts the discrepancy. Trusting a denormalised counter that is
-- demonstrably inconsistent with the transaction table would quietly corrupt
-- every LTV and repeat-rate number in the project.

CREATE OR REPLACE TABLE staging.stg_customers AS
SELECT
    CAST(id AS BIGINT)                              AS customer_id,
    lower(nullif(trim(email), ''))                  AS email,

    -- Real mailbox domain. orders.csv masks this to @mail.com, which is what
    -- makes email unusable as a join key between the two sources.
    lower(regexp_extract(email, '@(.+)$', 1))       AS email_domain,

    first_name,
    last_name,

    CAST(order_count AS INTEGER)                    AS order_count_raw,
    CAST(total_spent AS DECIMAL(12, 2))             AS total_spent_raw,

    -- Shopify account status ('enabled'), not a geographic state. Renamed so
    -- nobody groups revenue by it expecting US states.
    state                                           AS account_state,

    currency,
    CAST(verified_email AS BOOLEAN)                 AS is_verified_email,
    CAST(accepts_marketing AS BOOLEAN)              AS accepts_marketing,
    email_marketing_consent_state                   AS consent_state,

    CAST(created_at AS TIMESTAMP)                   AS created_at,
    CAST(created_at AS DATE)                        AS signup_date,

    CAST(nullif(trim(_weld_synced), '') AS TIMESTAMP) AS synced_at
FROM read_csv(
    getvariable('data_dir') || '/customers.csv',
    header = true,
    timestampformat = '%Y-%m-%d %H:%M:%S'
);
