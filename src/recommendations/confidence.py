"""Confidence scoring.

Four inputs, combined deliberately unevenly. See CONFIDENCE_WEIGHTS in
src/config.py for why data quality multiplies the other three rather than
averaging with them.
"""

from __future__ import annotations

from src.config import (
    CONFIDENCE_PERSISTENCE_SATURATION_DAYS,
    CONFIDENCE_STRENGTH_SATURATION,
    CONFIDENCE_WEIGHTS,
)
from src.detection.signal import Signal
from src.recommendations.recommendation import ConfidenceBreakdown


def strength_score(signals: list[Signal]) -> float:
    """How large the move is, saturating so a huge move is not infinitely
    better evidence than a large one.

    Uses the largest supporting signal rather than the average: a
    recommendation justified by one decisive signal and one marginal one is as
    well-evidenced as the decisive signal alone, and averaging would penalise
    it for showing extra work.
    """
    if not signals:
        return 0.0
    largest = max(s.abs_magnitude for s in signals)
    return min(1.0, largest / CONFIDENCE_STRENGTH_SATURATION)


def persistence_score(signals: list[Signal]) -> float:
    """How long it has held.

    Uses the longest-running supporting signal. Persistence is the cheapest
    defence against acting on noise, which is why it is weighted separately
    from magnitude rather than folded into it.
    """
    if not signals:
        return 0.0
    longest = max(s.persistence_days for s in signals)
    return min(1.0, longest / CONFIDENCE_PERSISTENCE_SATURATION_DAYS)


def corroboration_score(checks: dict[str, bool]) -> float:
    """Fraction of the rule's independent confirmations that hold.

    Each rule declares what would have to be true besides the triggering signal
    for its action to make sense. For the budget reallocation that includes the
    destination channel still being healthy and having demonstrated capacity to
    absorb more spend. Those are separate questions from "is Meta expensive",
    and a rule that cannot answer them should be less confident.

    An empty check set scores 0.5 rather than 0 or 1: no corroboration was
    sought, so there is neither support nor contradiction, and neither
    rewarding nor punishing that is honest.
    """
    if not checks:
        return 0.5
    return sum(1 for passed in checks.values() if passed) / len(checks)


def data_quality_score(signals: list[Signal]) -> float:
    """Trust in the underlying data.

    Uses the *worst* supporting signal, not the average. A recommendation is
    only as sound as its shakiest input, and averaging would let good data
    launder bad: a solid signal paired with one built on unusable data would
    score a comfortable 0.5 and pass a gate it should fail.
    """
    if not signals:
        return 0.0
    return min(s.data_quality_score for s in signals)


def score_confidence(
    signals: list[Signal],
    corroborating_checks: dict[str, bool] | None = None,
    notes: list[str] | None = None,
) -> ConfidenceBreakdown:
    checks = corroborating_checks or {}
    strength = strength_score(signals)
    persistence = persistence_score(signals)
    corroboration = corroboration_score(checks)
    quality = data_quality_score(signals)

    evidence = (
        CONFIDENCE_WEIGHTS["strength"] * strength
        + CONFIDENCE_WEIGHTS["persistence"] * persistence
        + CONFIDENCE_WEIGHTS["corroboration"] * corroboration
    )

    combined_notes = list(notes or [])
    for name, passed in checks.items():
        if not passed:
            combined_notes.append(f"Corroborating check failed: {name}")
    if quality < 0.5:
        combined_notes.append(
            f"Data quality of {quality:.2f} caps confidence regardless of signal strength"
        )

    return ConfidenceBreakdown(
        strength=strength,
        persistence=persistence,
        corroboration=corroboration,
        data_quality=quality,
        evidence_score=evidence,
        overall=evidence * quality,
        notes=combined_notes,
    )
