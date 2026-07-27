-- Data quality: every assertion the warehouse makes about itself.
--
-- Each row is one check. The design point worth explaining is the distinction
-- between severity and is_known_issue.
--
--   severity = 'error'    a guarantee the warehouse depends on. If one of
--                         these fails, downstream numbers are wrong and the
--                         build should be treated as broken.
--
--   is_known_issue        the check tests something already understood to be
--                         imperfect in the source. These are expected to fail.
--                         Their value is not the pass/fail bit but the
--                         observed number, which is fed into the quality score
--                         and from there into recommendation confidence.
--
-- Without that second category there are only two bad options: assert things
-- known to be false and ship a permanently red build that everyone learns to
-- ignore, or not assert them at all and lose the ability to notice when a
-- known 27% attribution gap quietly becomes 60%. Quantifying a known problem
-- on every build is what lets confidence respond to it automatically.
--
-- subject_metric ties each check to the metric family it underwrites, which is
-- how 02_dq_metric_quality turns these rows into per-metric scores.

CREATE OR REPLACE TABLE dq.check_results AS

-- === Referential integrity ==============================================
SELECT
    'order_lines_have_parent_order'                 AS check_name,
    'referential_integrity'                         AS check_category,
    'error'                                         AS severity,
    FALSE                                           AS is_known_issue,
    'revenue'                                       AS subject_metric,
    (SELECT COUNT(*) FROM core.fct_order_lines l
      LEFT JOIN core.fct_orders o USING (order_id) WHERE o.order_id IS NULL)::DOUBLE AS observed_value,
    0.0                                             AS threshold,
    'Order lines referencing a missing order'       AS description

UNION ALL SELECT
    'order_lines_have_known_variant', 'referential_integrity', 'error', FALSE, 'product_velocity',
    (SELECT COUNT(*) FROM staging.stg_order_lines l
      LEFT JOIN core.dim_product p USING (variant_id) WHERE p.variant_id IS NULL)::DOUBLE,
    0.0, 'Order lines referencing a variant absent from the catalogue'

UNION ALL SELECT
    'orders_have_known_customer', 'referential_integrity', 'error', FALSE, 'retention',
    (SELECT COUNT(*) FROM core.fct_orders o
      LEFT JOIN core.dim_customer c USING (customer_id) WHERE c.customer_id IS NULL)::DOUBLE,
    0.0, 'Orders referencing a customer absent from the customer table'

-- === Uniqueness =========================================================
UNION ALL SELECT
    'order_id_is_unique', 'uniqueness', 'error', FALSE, 'revenue',
    (SELECT COUNT(*) - COUNT(DISTINCT order_id) FROM core.fct_orders)::DOUBLE,
    0.0, 'Duplicate order ids'

UNION ALL SELECT
    'order_line_id_is_unique', 'uniqueness', 'error', FALSE, 'revenue',
    (SELECT COUNT(*) - COUNT(DISTINCT order_line_id) FROM core.fct_order_lines)::DOUBLE,
    0.0, 'Duplicate order line ids'

UNION ALL SELECT
    'customer_id_is_unique', 'uniqueness', 'error', FALSE, 'retention',
    (SELECT COUNT(*) - COUNT(DISTINCT customer_id) FROM core.dim_customer)::DOUBLE,
    0.0, 'Duplicate customer ids'

UNION ALL SELECT
    'ad_spend_grain_is_unique', 'uniqueness', 'error', FALSE, 'channel_efficiency',
    (SELECT COUNT(*) - COUNT(DISTINCT (channel, campaign_id, ad_date)) FROM core.fct_ad_spend)::DOUBLE,
    0.0, 'Duplicate channel/campaign/day rows in ad spend'

-- === Financial reconciliation ===========================================
UNION ALL SELECT
    'order_lines_tie_to_order_header', 'reconciliation', 'error', FALSE, 'revenue',
    (SELECT COUNT(*) FROM core.fct_orders o
       JOIN (SELECT order_id, SUM(line_net_revenue) net FROM core.fct_order_lines GROUP BY 1) l
         USING (order_id)
      WHERE abs(o.net_revenue - o.shipping_price - l.net) > 0.05)::DOUBLE,
    0.0, 'Orders where summed line revenue does not tie to the header, net of untaxed shipping'

UNION ALL SELECT
    'vat_is_20pct_of_goods_subtotal', 'reconciliation', 'error', FALSE, 'revenue',
    (SELECT COUNT(*) FROM staging.stg_orders
      WHERE abs(total_tax - round(subtotal_price / 6.0, 2)) > 0.015)::DOUBLE,
    0.0, 'Orders whose VAT is not 20% of the discounted goods subtotal (shipping untaxed)'

UNION ALL SELECT
    'order_header_arithmetic_holds', 'reconciliation', 'error', FALSE, 'revenue',
    (SELECT COUNT(*) FROM staging.stg_orders
      WHERE abs(total_price_gross - (line_items_price - total_discounts + shipping_price)) > 0.005)::DOUBLE,
    0.0, 'Orders where total does not equal line items less discounts plus shipping'

UNION ALL SELECT
    'customer_total_spent_reconciles', 'reconciliation', 'warning', TRUE, 'retention',
    (SELECT COUNT(*) FROM core.dim_customer c
       LEFT JOIN (SELECT customer_id, SUM(gross_revenue) g FROM core.fct_orders
                   WHERE is_valid_order GROUP BY 1) o USING (customer_id)
      WHERE abs(c.total_spent_raw - COALESCE(o.g, 0)) > 0.02)::DOUBLE,
    0.0, 'Customers whose exported total_spent disagrees with summed valid orders'

UNION ALL SELECT
    'customer_order_count_reconciles', 'reconciliation', 'warning', TRUE, 'retention',
    (SELECT COUNT(*) FROM core.dim_customer WHERE order_count_raw <> orders_count)::DOUBLE,
    0.0, 'Customers whose exported order_count disagrees with a recomputed count. '
      || 'Expected: the export counts cancelled orders while total_spent excludes them, '
      || 'which is why neither exported column is used downstream'

-- === Completeness =======================================================
UNION ALL SELECT
    'meta_spend_has_no_missing_days', 'completeness', 'warning', TRUE, 'channel_efficiency',
    (SELECT 365 - COUNT(DISTINCT ad_date) FROM core.fct_ad_spend WHERE channel = 'Meta')::DOUBLE,
    0.0, 'Calendar days with no Meta spend row. Expected: 2025-03-15 and 2025-03-16 are absent '
      || 'for all six campaigns, roughly 793 GBP of unrecorded spend'

UNION ALL SELECT
    'google_spend_has_no_missing_days', 'completeness', 'error', FALSE, 'channel_efficiency',
    (SELECT 365 - COUNT(DISTINCT ad_date) FROM core.fct_ad_spend WHERE channel = 'Google')::DOUBLE,
    0.0, 'Calendar days with no Google spend row'

UNION ALL SELECT
    'every_day_has_orders', 'completeness', 'error', FALSE, 'revenue',
    (SELECT COUNT(*) FROM semantic.daily_business_metrics WHERE orders = 0)::DOUBLE,
    0.0, 'Trading days with no orders at all'

UNION ALL SELECT
    'products_have_cost_and_price', 'completeness', 'error', FALSE, 'margin',
    (SELECT COUNT(*) FROM core.dim_product WHERE unit_cost IS NULL OR price_ex_vat IS NULL)::DOUBLE,
    0.0, 'Variants missing cost or price, which would silently break margin'

-- === Validity ===========================================================
UNION ALL SELECT
    'no_negative_revenue', 'validity', 'error', FALSE, 'revenue',
    (SELECT COUNT(*) FROM core.fct_orders WHERE net_revenue < 0)::DOUBLE,
    0.0, 'Orders with negative net revenue'

UNION ALL SELECT
    'no_negative_quantity', 'validity', 'error', FALSE, 'product_velocity',
    (SELECT COUNT(*) FROM core.fct_order_lines WHERE quantity <= 0)::DOUBLE,
    0.0, 'Order lines with non-positive quantity'

UNION ALL SELECT
    'no_negative_unit_margin', 'validity', 'warning', FALSE, 'margin',
    (SELECT COUNT(*) FROM core.dim_product WHERE unit_margin <= 0)::DOUBLE,
    0.0, 'Variants sold at or below cost'

UNION ALL SELECT
    'orders_within_analysis_window', 'validity', 'error', FALSE, 'revenue',
    (SELECT COUNT(*) FROM core.fct_orders
      WHERE order_date < DATE '2024-07-01' OR order_date > DATE '2025-06-30')::DOUBLE,
    0.0, 'Orders dated outside the stated 12-month window'

-- === Attribution ========================================================
UNION ALL SELECT
    'order_email_matches_customer_email', 'validity', 'warning', TRUE, 'attribution',
    (SELECT COUNT(*) FROM core.fct_orders o JOIN staging.stg_customers c USING (customer_id)
       JOIN staging.stg_orders so USING (order_id)
      WHERE so.email_masked IS DISTINCT FROM c.email)::DOUBLE,
    0.0, 'Orders whose email disagrees with the customer record. Expected: orders.csv is '
      || 'redacted to @mail.com, which is why email is never used as a join key'

UNION ALL SELECT
    'attribution_coverage', 'attribution', 'warning', TRUE, 'attribution',
    (SELECT COUNT(*) FILTER (WHERE channel_group = 'Unattributed')::DOUBLE / COUNT(*)
       FROM core.fct_orders WHERE is_valid_order),
    0.10, 'Share of valid orders with no usable referrer. Expected around 0.27, which is why '
      || 'channel CAC is reported as a range rather than a point estimate'

UNION ALL SELECT
    'paid_traffic_has_spend_data', 'attribution', 'warning', TRUE, 'channel_efficiency',
    (SELECT COUNT(*) FROM core.fct_orders
      WHERE is_valid_order AND channel = 'TikTok')::DOUBLE,
    0.0, 'Orders attributed to a paid channel with no spend file. Expected: TikTok sends '
      || 'traffic but supplies no cost data, so its CAC is uncomputable'

-- === Email ==============================================================
UNION ALL SELECT
    'email_flows_cover_full_window', 'completeness', 'error', FALSE, 'email_engagement',
    (SELECT 12 - COUNT(DISTINCT date_trunc('month', date_day)) FROM semantic.email_flow_performance)::DOUBLE,
    0.0, 'Months with no email flow activity reported'

UNION ALL SELECT
    'email_funnel_is_monotonic', 'validity', 'error', FALSE, 'email_engagement',
    (SELECT COUNT(*) FROM core.fct_email_flow
      WHERE unique_clicks > unique_opens OR unique_orders > unique_clicks
         OR unique_opens > recipients)::DOUBLE,
    0.0, 'Flow rows where the funnel inverts, for example more clicks than opens'

-- === Behavioural plausibility ===========================================
-- These are the checks that caught the retention artifact. They assert that
-- the data behaves like a real business, which is a different question from
-- whether it is internally consistent, and the only kind of check that could
-- have caught this.
UNION ALL SELECT
    'recent_cohorts_show_repeat_purchasing', 'plausibility', 'warning', TRUE, 'ltv',
    (SELECT COUNT(*) FROM semantic.cohort_repeat_windows
      WHERE has_full_90d_window AND repeat_rate_90d < 0.01)::DOUBLE,
    0.0, 'Mature cohorts with effectively zero 90-day repeat rate. Expected: every cohort from '
      || 'December 2024 onward, which is a generation artifact rather than churn and is why '
      || 'LTV is excluded from the unit economics'

UNION ALL SELECT
    'repeat_purchasing_spread_across_cohorts', 'plausibility', 'warning', TRUE, 'ltv',
    (SELECT COUNT(*) FILTER (WHERE c.cohort_month <= DATE '2024-09-01')::DOUBLE / COUNT(*)
       FROM core.fct_orders o JOIN core.dim_customer c USING (customer_id)
      WHERE o.is_valid_order AND NOT o.is_first_order AND o.order_date >= DATE '2025-01-01'),
    0.60, 'Share of 2025 repeat orders coming from the first three cohorts. Expected around '
      || '0.98, far beyond what a real customer base would produce'
;

-- Derive pass/fail once, so the rule lives in one place. Every check is framed
-- so that a lower observed value is better.
ALTER TABLE dq.check_results ADD COLUMN passed BOOLEAN;
UPDATE dq.check_results SET passed = (observed_value <= threshold);
