-- Data quality: a 0-1 trust score per metric family, consumed by the
-- recommendation engine's confidence calculation.
--
-- This is the mechanism that stops the engine acting confidently on broken
-- inputs. A signal computed from a metric family scoring 0.4 cannot produce a
-- high-confidence recommendation no matter how large or persistent the move,
-- because data_quality is a weighted term in the confidence blend.
--
-- Two rules:
--
--   A failing error-severity check zeroes the family. These are guarantees the
--   warehouse depends on, so if one breaks the numbers are not merely uncertain,
--   they are wrong, and no recommendation should be built on them.
--
--   A failing known-issue check deducts an explicit penalty. Penalties are set
--   below by hand rather than derived, because how much a given flaw should
--   erode trust is a judgement call, and burying a judgement call in a formula
--   makes it harder to argue with. They are sized by how directly the flaw
--   affects decisions made from that metric family: the attribution gap costs
--   0.25 because it moves CAC materially, while the redacted order email costs
--   0.05 because it is fully understood and already worked around.
--
-- The retention and LTV families are deliberately driven near zero. That is
-- the intended outcome: it is what mechanically prevents the engine from
-- issuing a retention recommendation off artifact data, rather than relying on
-- a human to remember the caveat.

CREATE OR REPLACE TABLE dq.metric_quality AS
WITH penalties AS (
    SELECT * FROM (VALUES
        -- Attribution flaws, sized by how far they move a CAC decision.
        ('attribution_coverage',                      0.25),
        ('paid_traffic_has_spend_data',               0.10),
        ('order_email_matches_customer_email',        0.05),
        -- Spend completeness: two missing days in twelve months.
        ('meta_spend_has_no_missing_days',            0.10),
        -- Customer export inconsistencies, worked around by recomputation.
        ('customer_total_spent_reconciles',           0.05),
        ('customer_order_count_reconciles',           0.10),
        -- The retention artifact. Priced to make the family unusable, because
        -- it is.
        ('recent_cohorts_show_repeat_purchasing',     0.60),
        ('repeat_purchasing_spread_across_cohorts',   0.40)
    ) AS t(check_name, penalty)
),
scored AS (
    SELECT
        r.subject_metric                                            AS metric_family,
        COUNT(*)                                                    AS checks_run,
        COUNT(*) FILTER (WHERE r.passed)                            AS checks_passed,
        COUNT(*) FILTER (WHERE NOT r.passed)                        AS checks_failed,
        COUNT(*) FILTER (WHERE NOT r.passed AND r.severity = 'error')       AS errors_failed,
        COUNT(*) FILTER (WHERE NOT r.passed AND r.is_known_issue)           AS known_issues_failed,
        COALESCE(SUM(CASE WHEN NOT r.passed THEN p.penalty END), 0) AS total_penalty
    FROM dq.check_results r
    LEFT JOIN penalties p USING (check_name)
    GROUP BY 1
)
SELECT
    metric_family,
    checks_run,
    checks_passed,
    checks_failed,
    errors_failed,
    known_issues_failed,
    total_penalty,
    CASE
        WHEN errors_failed > 0 THEN 0.0
        ELSE GREATEST(0.0, 1.0 - total_penalty)
    END                                                             AS quality_score,
    CASE
        WHEN errors_failed > 0 THEN 'broken'
        WHEN 1.0 - total_penalty >= 0.90 THEN 'trusted'
        WHEN 1.0 - total_penalty >= 0.70 THEN 'usable'
        WHEN 1.0 - total_penalty >= 0.40 THEN 'degraded'
        ELSE 'unusable'
    END                                                             AS quality_band
FROM scored
ORDER BY quality_score, metric_family;
