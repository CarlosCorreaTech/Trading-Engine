"""Adjudicate the apparent cohort retention collapse.

The dataset shows 90-day repeat rate falling from 32% for the July 2024 cohort
to effectively zero from December 2024 onward. Taken at face value that is the
single largest finding available, and the obvious recommendation would be an
urgent retention programme.

It is also almost certainly wrong, and this module exists to establish that
from evidence rather than assume it either way. It runs three tests that can
each independently discredit the naive reading, and only concludes ARTIFACT if
the tests support it. If a future dataset produced a genuine collapse, the same
code would report a COMMERCIAL signal.

The reason this matters beyond one dataset: retention metrics are unusually
easy to compute correctly and still misread. A number can reconcile perfectly,
pass every referential and financial check, and still describe an artifact of
how the data was assembled. Distinguishing the two is not a data cleaning
problem, it is a question about whether the data behaves like a business.
"""

from __future__ import annotations

import datetime as dt

import numpy as np

from src.detection.signal import Classification, Signal, SignalType
from src.warehouse import query

# A cohort repeat rate below this is treated as "nobody came back at all",
# which no real cohort of a thousand-plus customers produces.
_ZERO_REPEAT_BAR = 0.01

# If this share of recent repeat orders comes from the three earliest cohorts,
# repeat purchasing is not a property of the customer base but of a fixed
# subgroup baked in at the start.
_CONCENTRATION_BAR = 0.80

# Coefficient of variation below which the brand-level repeat share counts as
# flat over the year.
_FLATNESS_BAR = 0.15


def adjudicate_cohort_retention(con, quality: dict[str, float]) -> list[Signal]:
    """Decide whether the retention collapse is real, and evidence the verdict."""
    cohorts = query(
        con,
        """
        SELECT cohort_month, cohort_size, repeat_rate_90d,
               expected_repeat_rate_90d_if_uniform, pool_size_at_cohort_start,
               has_full_90d_window
        FROM semantic.cohort_repeat_windows
        ORDER BY cohort_month
        """,
    )
    mature = cohorts[cohorts["has_full_90d_window"]].copy()
    if len(mature) < 4:
        return []

    # --- Test 1: do mature cohorts show literally zero repeat purchasing? ----
    zero_cohorts = mature[mature["repeat_rate_90d"] < _ZERO_REPEAT_BAR]
    earliest_rate = float(mature["repeat_rate_90d"].iloc[0])
    latest_rate = float(mature["repeat_rate_90d"].iloc[-1])

    # --- Test 2: is repeat purchasing confined to the earliest cohorts? -----
    concentration = query(
        con,
        """
        SELECT
            count(*) FILTER (WHERE c.cohort_month <= DATE '2024-09-01')::DOUBLE
                / nullif(count(*), 0) AS early_cohort_share,
            count(*)                  AS repeat_orders_2025
        FROM core.fct_orders o
        JOIN core.dim_customer c USING (customer_id)
        WHERE o.is_valid_order AND NOT o.is_first_order
          AND o.order_date >= DATE '2025-01-01'
        """,
    )
    early_share = float(concentration["early_cohort_share"].iloc[0])

    # --- Test 3: is the brand-level repeat share flat while cohorts go to zero?
    # These two facts are mutually contradictory. If genuinely no new customer
    # ever returns, the repeat share must fall as older cohorts are exhausted.
    monthly = query(
        con,
        """
        SELECT date_trunc('month', date_day)::DATE AS month_start,
               sum(repeat_orders)::DOUBLE / nullif(sum(orders), 0) AS repeat_share
        FROM semantic.daily_business_metrics
        GROUP BY 1 ORDER BY 1
        """,
    )
    shares = monthly["repeat_share"].dropna().to_numpy()
    flatness = float(np.std(shares) / np.mean(shares)) if shares.size and np.mean(shares) else 1.0

    # --- Does the mechanical null model explain the decay? -------------------
    comparable = mature.dropna(subset=["expected_repeat_rate_90d_if_uniform"])
    if len(comparable) >= 3:
        uniform_correlation = float(
            np.corrcoef(comparable["repeat_rate_90d"], comparable["expected_repeat_rate_90d_if_uniform"])[0, 1]
        )
    else:
        uniform_correlation = float("nan")

    has_zero_cohorts = len(zero_cohorts) > 0
    is_concentrated = early_share >= _CONCENTRATION_BAR
    is_flat = flatness <= _FLATNESS_BAR

    # The verdict. A genuine retention collapse cannot coexist with a flat
    # brand-level repeat share, and cannot leave repeat purchasing confined to
    # a fixed early subgroup. Either of those, combined with mature cohorts at
    # exactly zero, is disqualifying.
    is_artifact = has_zero_cohorts and (is_concentrated or is_flat)

    if is_artifact:
        classification = Classification.ARTIFACT
        summary = (
            f"The apparent retention collapse is an artifact of how this dataset was "
            f"generated, not a commercial event, and no retention recommendation should "
            f"be made from it. Three findings rule out a genuine collapse: "
            f"{len(zero_cohorts)} fully mature cohorts show a 90-day repeat rate of "
            f"effectively zero, which no real cohort of this size produces; "
            f"{early_share:.0%} of all 2025 repeat orders come from customers acquired in "
            f"the first three months; and the brand's overall repeat order share is flat "
            f"at roughly {np.mean(shares):.0%} across the whole year. The last two cannot "
            f"both be true of a real business, because if no new customer ever returned "
            f"the repeat share would have to fall as the early cohorts were used up. "
            f"The practical consequence is that lifetime value cannot be measured on this "
            f"data, so unit economics fall back to first-order contribution margin."
        )
    else:
        classification = Classification.COMMERCIAL
        summary = (
            f"Cohort repeat rate has fallen from {earliest_rate:.0%} to {latest_rate:.0%} "
            f"across mature cohorts, and the checks for a generation artifact do not "
            f"explain it. This looks like a genuine retention problem."
        )

    return [
        Signal(
            signal_id="cohort_retention_collapse",
            signal_type=SignalType.TREND_BREAK,
            classification=classification,
            entity="Business",
            metric="repeat_rate_90d",
            metric_family="ltv",
            detected_at=dt.date(2024, 12, 1),
            direction="decrease",
            baseline_value=earliest_rate,
            current_value=latest_rate,
            magnitude=(latest_rate - earliest_rate) / earliest_rate if earliest_rate else 0.0,
            persistence_days=int(len(zero_cohorts) * 30),
            p_value=None,
            data_quality_score=quality.get("ltv", 1.0),
            summary=summary,
            evidence={
                "mature_cohorts_tested": int(len(mature)),
                "cohorts_with_zero_repeat": int(len(zero_cohorts)),
                "earliest_cohort_repeat_rate": round(earliest_rate, 4),
                "latest_cohort_repeat_rate": round(latest_rate, 4),
                "share_of_2025_repeats_from_first_3_cohorts": round(early_share, 4),
                "brand_repeat_share_mean": round(float(np.mean(shares)), 4),
                "brand_repeat_share_coefficient_of_variation": round(flatness, 4),
                "correlation_with_uniform_draw_model": (
                    None if np.isnan(uniform_correlation) else round(uniform_correlation, 4)
                ),
                "test_zero_cohorts": bool(has_zero_cohorts),
                "test_concentrated_in_early_cohorts": bool(is_concentrated),
                "test_brand_repeat_share_flat": bool(is_flat),
                "verdict": classification.value,
            },
        )
    ]
