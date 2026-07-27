-- Core: date spine covering the full analysis window.
--
-- A dense calendar matters more than usual here. Meta is missing two days of
-- spend, and several metrics are computed as ratios of daily series; without a
-- spine, a missing day silently disappears from a rolling window instead of
-- being visible as a hole. Every daily model joins to this table so gaps stay
-- explicit.
--
-- is_peak_season flags November and December. That period carries genuinely
-- higher volume (Dec revenue is ~1.6x the trough), and the detectors must
-- deseasonalise against it or every anomaly test fires at Christmas and again
-- in January when volume falls back.

CREATE OR REPLACE TABLE core.dim_date AS
SELECT
    d                                               AS date_day,
    year(d)                                         AS year,
    month(d)                                        AS month,
    strftime(d, '%Y-%m')                            AS year_month,
    quarter(d)                                      AS quarter,
    day(d)                                          AS day_of_month,
    dayofweek(d)                                    AS day_of_week,
    dayname(d)                                      AS day_name,
    (dayofweek(d) IN (0, 6))                        AS is_weekend,
    date_trunc('week', d)::DATE                     AS week_start,
    date_trunc('month', d)::DATE                    AS month_start,
    (month(d) IN (11, 12))                          AS is_peak_season,
    -- Days since the start of the window, used as the x-axis for trend fits.
    (d - DATE '2024-07-01')                         AS day_index
FROM (
    SELECT unnest(generate_series(DATE '2024-07-01', DATE '2025-06-30', INTERVAL 1 DAY))::DATE AS d
);
