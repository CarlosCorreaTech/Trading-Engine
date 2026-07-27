"""The Signal record produced by every detector.

One uniform shape for every detector matters more than it might appear. The
recommendation engine consumes signals generically, so a new detector becomes
actionable without touching the rules engine, and the confidence calculation
applies identically to all of them rather than being reinvented per metric.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum


class SignalType(str, Enum):
    """What kind of movement was detected."""

    TREND_BREAK = "trend_break"
    THRESHOLD_BREACH = "threshold_breach"
    VELOCITY_CHANGE = "velocity_change"
    RATIO_DIVERGENCE = "ratio_divergence"
    INVENTORY_RISK = "inventory_risk"
    COMPOSITION_SHIFT = "composition_shift"


class Classification(str, Enum):
    """What the movement means, which decides what should happen next.

    This is the distinction the brief asks for, and getting it wrong is
    expensive in both directions: treating a tracking outage as a commercial
    collapse triggers pointless spend, while treating a real collapse as a
    tracking issue means doing nothing while the business bleeds.

    COMMERCIAL   Something real changed in the market or the business.
                 Eligible to become a recommendation.

    DATA_QUALITY The measurement changed, not the thing being measured.
                 Routes to a human as an instrumentation problem. Never
                 becomes a commercial recommendation.

    ARTIFACT     The pattern is an artifact of how the data was produced and
                 describes no real-world event at all. Suppressed from
                 recommendations entirely and reported as a caveat.

    EXPLAINED    Real, but fully accounted for by another signal already
                 surfaced. Suppressed to stop one event generating four alerts
                 and to avoid double-counting its impact.
    """

    COMMERCIAL = "commercial"
    DATA_QUALITY = "data_quality"
    ARTIFACT = "artifact"
    EXPLAINED = "explained"


@dataclass(frozen=True)
class Signal:
    """A detected movement, with the evidence needed to judge it."""

    signal_id: str
    signal_type: SignalType
    classification: Classification

    entity: str
    """What moved: a channel, a SKU, an email flow, or the business overall."""

    metric: str
    metric_family: str
    """Ties the signal to a dq.metric_quality family, which is how data quality
    reaches the confidence score."""

    detected_at: dt.date
    """When the change is judged to have begun, not when it was noticed. A
    recommendation that says 'CPC has been climbing since 6 April' is far more
    actionable than one that says 'CPC is high today'."""

    direction: str
    baseline_value: float
    current_value: float
    magnitude: float
    """Relative change against baseline, signed. 0.5 means 50% higher."""

    persistence_days: int
    """How long the move has held. The single best guard against noise."""

    data_quality_score: float
    summary: str
    """Plain English, written for someone who will not read the code."""

    p_value: float | None = None
    evidence: dict = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        """Only genuine commercial events may become recommendations."""
        return self.classification is Classification.COMMERCIAL

    @property
    def abs_magnitude(self) -> float:
        return abs(self.magnitude)

    def to_row(self) -> dict:
        """Flat dict for tabular display."""
        return {
            "signal_id": self.signal_id,
            "type": self.signal_type.value,
            "classification": self.classification.value,
            "entity": self.entity,
            "metric": self.metric,
            "detected_at": self.detected_at,
            "direction": self.direction,
            "baseline": round(self.baseline_value, 4),
            "current": round(self.current_value, 4),
            "magnitude": round(self.magnitude, 4),
            "persistence_days": self.persistence_days,
            "p_value": None if self.p_value is None else round(self.p_value, 6),
            "data_quality": round(self.data_quality_score, 2),
            "summary": self.summary,
        }
