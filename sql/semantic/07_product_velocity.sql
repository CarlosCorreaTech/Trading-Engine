-- Semantic: daily SKU velocity and forward inventory cover.
--
-- Grain is variant x day, on the full date spine, so a SKU that sells nothing
-- on a Tuesday contributes a real zero to its rolling average rather than
-- vanishing from the window and overstating velocity.
--
-- Inventory needs a caveat that shapes how the recommendation is worded. The
-- product feed carries a single current inventory_quantity with no history, so
-- stock cannot be reconstructed for past dates. Cover is therefore only
-- meaningful on the final day of data: it answers "given what is on the shelf
-- now and how fast it has been selling lately, how long until it runs out",
-- and nothing about whether the brand has stocked out before.
--
-- Cover uses 28-day trailing velocity rather than 7-day. Seven days is too
-- jumpy for a purchasing decision that commits cash for months, and the
-- difference is not academic: for the surging Vitamin D3 SKUs the two windows
-- disagree by enough to change the reorder quantity.

CREATE OR REPLACE TABLE semantic.product_velocity AS
WITH line_daily AS (
    SELECT
        order_date                  AS date_day,
        variant_id,
        SUM(quantity)               AS units,
        SUM(line_net_revenue)       AS net_revenue,
        SUM(line_gross_profit)      AS gross_profit,
        COUNT(DISTINCT order_id)    AS orders
    FROM core.fct_order_lines
    WHERE is_valid_order
    GROUP BY 1, 2
),
spine AS (
    SELECT d.date_day, p.variant_id
    FROM core.dim_date d
    CROSS JOIN core.dim_product p
),
dense AS (
    SELECT
        s.date_day,
        s.variant_id,
        COALESCE(l.units, 0)        AS units,
        COALESCE(l.net_revenue, 0)  AS net_revenue,
        COALESCE(l.gross_profit, 0) AS gross_profit,
        COALESCE(l.orders, 0)       AS orders
    FROM spine s
    LEFT JOIN line_daily l
           ON l.date_day = s.date_day AND l.variant_id = s.variant_id
),
rolled AS (
    SELECT
        d.*,
        AVG(units) OVER w7   AS units_ma7,
        AVG(units) OVER w28  AS units_ma28,
        SUM(units) OVER w28  AS units_28d,
        -- Baseline is the 28 days ending four weeks ago, so a surge is
        -- compared against a period it has not already contaminated.
        AVG(units) OVER w_baseline AS units_baseline_ma28
    FROM dense d
    WINDOW
        w7  AS (PARTITION BY variant_id ORDER BY date_day RANGE BETWEEN INTERVAL 6 DAY PRECEDING AND CURRENT ROW),
        w28 AS (PARTITION BY variant_id ORDER BY date_day RANGE BETWEEN INTERVAL 27 DAY PRECEDING AND CURRENT ROW),
        w_baseline AS (PARTITION BY variant_id ORDER BY date_day
                       RANGE BETWEEN INTERVAL 55 DAY PRECEDING AND INTERVAL 28 DAY PRECEDING)
),
last_day AS (SELECT MAX(date_day) AS snapshot_date FROM core.dim_date)
SELECT
    r.date_day,
    r.variant_id,
    p.sku,
    p.product_title,
    p.variant_title,
    p.variant_label,
    p.product_type,
    p.price_ex_vat,
    p.unit_cost,
    p.unit_margin,
    p.inventory_snapshot,

    r.units,
    r.orders,
    r.net_revenue,
    r.gross_profit,
    r.units_ma7,
    r.units_ma28,
    r.units_28d,
    r.units_baseline_ma28,

    CASE WHEN r.units_baseline_ma28 > 0
         THEN r.units_ma28 / r.units_baseline_ma28 - 1 END      AS velocity_change_vs_baseline,

    -- Only meaningful on the last day of data; NULL elsewhere so nobody plots
    -- a cover trend that does not exist.
    CASE WHEN r.date_day = ld.snapshot_date AND r.units_ma28 > 0
         THEN p.inventory_snapshot / r.units_ma28 END           AS days_of_cover,
    (r.date_day = ld.snapshot_date)                             AS is_current_snapshot
FROM rolled r
JOIN core.dim_product p USING (variant_id)
CROSS JOIN last_day ld
ORDER BY r.variant_id, r.date_day;
