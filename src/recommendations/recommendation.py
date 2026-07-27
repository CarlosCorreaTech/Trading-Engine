"""The Recommendation record and the vocabulary it uses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ActionType(str, Enum):
    BUDGET_REALLOCATION = "budget_reallocation"
    INVENTORY_PURCHASE = "inventory_purchase"
    INVESTIGATION = "investigation"
    FLOW_OPTIMISATION = "flow_optimisation"
    NO_ACTION = "no_action"


class Reversibility(str, Enum):
    """Whether a decision can be undone, which governs how much certainty it
    needs before the engine may act without a human.

    REVERSIBLE   Undoable within about a day, and the downside is bounded by
                 the amount committed. Shifting ad budget qualifies: if it is
                 wrong, you shift it back and the cost is a few days of
                 slightly worse efficiency.

    IRREVERSIBLE Commits cash that cannot be recalled. Buying stock qualifies:
                 the money is spent, the goods occupy space, they may expire,
                 and if demand was misread the only exit is discounting. The
                 mistake compounds instead of stopping.

    The asymmetry is the point. Two decisions with identical expected value
    deserve different treatment when one can be unwound and the other cannot.
    """

    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """Confidence with its workings shown.

    A single number invites blind trust or blanket dismissal. The breakdown
    lets a reader see that, say, a recommendation scores well on evidence and
    poorly on data quality, and argue with the specific part they disagree with.
    """

    strength: float
    persistence: float
    corroboration: float
    data_quality: float
    evidence_score: float
    overall: float
    notes: list[str] = field(default_factory=list)

    def explain(self) -> str:
        return (
            f"strength {self.strength:.2f}, persistence {self.persistence:.2f}, "
            f"corroboration {self.corroboration:.2f} "
            f"-> evidence {self.evidence_score:.2f}, "
            f"scaled by data quality {self.data_quality:.2f} "
            f"= {self.overall:.2f}"
        )


@dataclass(frozen=True)
class Recommendation:
    """A proposed action, its justification, and how sure the engine is."""

    recommendation_id: str
    title: str
    action_type: ActionType
    reversibility: Reversibility

    action: str
    """What to do, concretely enough to execute without further interpretation."""

    rationale: str
    """Why, in language a non-technical stakeholder can follow."""

    confidence: ConfidenceBreakdown
    supporting_signal_ids: list[str]

    parameters: dict[str, Any] = field(default_factory=dict)
    """Machine-readable sizing, and the inputs the simulation needs."""

    caveats: list[str] = field(default_factory=list)
    """What could make this wrong. Stated up front rather than discovered later."""

    @property
    def is_actionable(self) -> bool:
        return self.action_type is not ActionType.NO_ACTION

    def to_row(self) -> dict:
        return {
            "recommendation_id": self.recommendation_id,
            "title": self.title,
            "action_type": self.action_type.value,
            "reversibility": self.reversibility.value,
            "confidence": round(self.confidence.overall, 3),
            "signals": ", ".join(self.supporting_signal_ids),
        }
