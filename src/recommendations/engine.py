"""Run the rule set against a signal set."""

from __future__ import annotations

import pandas as pd

from src.detection.signal import Signal
from src.recommendations.recommendation import ActionType, Recommendation
from src.recommendations.rules import RULES


def recommend(signals: list[Signal], con) -> list[Recommendation]:
    """Apply every rule and return the resulting recommendations.

    Ordered with actionable items first, most confident first, and the
    deliberate non-actions last. Someone reading this wants to know what to do
    before they want to know what was ruled out.
    """
    recommendations: list[Recommendation] = []
    for rule in RULES:
        recommendations.extend(rule(signals, con))

    return sorted(
        recommendations,
        key=lambda r: (r.action_type is ActionType.NO_ACTION, -r.confidence.overall),
    )


def recommendations_to_frame(recommendations: list[Recommendation]) -> pd.DataFrame:
    if not recommendations:
        return pd.DataFrame()
    return pd.DataFrame([r.to_row() for r in recommendations])
