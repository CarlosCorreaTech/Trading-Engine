"""Recommendation engine: signals in, confidence-scored actions out."""

from src.recommendations.engine import recommend, recommendations_to_frame
from src.recommendations.recommendation import (
    ActionType,
    ConfidenceBreakdown,
    Recommendation,
    Reversibility,
)

__all__ = [
    "ActionType",
    "ConfidenceBreakdown",
    "Recommendation",
    "Reversibility",
    "recommend",
    "recommendations_to_frame",
]
