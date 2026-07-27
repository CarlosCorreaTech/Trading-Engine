"""Run every detector and return the combined signal set.

Signals are returned for all classifications, not just actionable ones. The
suppressed signals carry as much information as the actionable ones: knowing
that AOV fell for a benign reason, or that a retention metric is unusable, is
what stops someone else acting on the same number next week.
"""

from __future__ import annotations

import pandas as pd

from src.detection.cohort_adjudication import adjudicate_cohort_retention
from src.detection.detectors import (
    detect_aov_decline,
    detect_cpc_inflation,
    detect_email_engagement_divergence,
    detect_inventory_risk,
    detect_payback_breach,
    detect_product_velocity_change,
)
from src.detection.signal import Classification, Signal
from src.warehouse import quality_scores

DETECTORS = (
    detect_cpc_inflation,
    detect_payback_breach,
    detect_product_velocity_change,
    detect_inventory_risk,
    detect_aov_decline,
    detect_email_engagement_divergence,
    adjudicate_cohort_retention,
)


def detect_all(con) -> list[Signal]:
    """Run all detectors against the warehouse.

    Ordered by classification then by size, so the actionable commercial
    signals surface first and the largest of those first, which is the order a
    reader wants rather than the order the detectors happen to run in.
    """
    quality = quality_scores(con)

    signals: list[Signal] = []
    for detector in DETECTORS:
        signals.extend(detector(con, quality))

    priority = {
        Classification.COMMERCIAL: 0,
        Classification.DATA_QUALITY: 1,
        Classification.ARTIFACT: 2,
        Classification.EXPLAINED: 3,
    }
    return sorted(signals, key=lambda s: (priority[s.classification], -s.abs_magnitude))


def signals_to_frame(signals: list[Signal]) -> pd.DataFrame:
    """Tabular view for the notebook and CLI output."""
    if not signals:
        return pd.DataFrame()
    return pd.DataFrame([s.to_row() for s in signals])
