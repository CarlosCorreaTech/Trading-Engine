"""The autonomy gate: what the engine may do alone, and what it must hand over."""

from src.autonomy.gate import (
    GateCheck,
    GateDecision,
    Verdict,
    decisions_to_frame,
    evaluate,
    evaluate_all,
)
from src.autonomy.loss import expected_shortfall, loss_adjusted_value

__all__ = [
    "GateCheck",
    "GateDecision",
    "Verdict",
    "decisions_to_frame",
    "evaluate",
    "evaluate_all",
    "expected_shortfall",
    "loss_adjusted_value",
]
