"""Tests for confidence scoring.

The behaviour worth pinning down here is not the arithmetic but the design
decisions: that bad data caps confidence rather than being averaged away, and
that the weakest input governs rather than the average one. Both are easy to
"simplify" later into something that looks equivalent and is not.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.detection.signal import Classification, Signal, SignalType
from src.recommendations.confidence import (
    corroboration_score,
    data_quality_score,
    persistence_score,
    score_confidence,
    strength_score,
)


def make_signal(
    magnitude: float = 1.0,
    persistence_days: int = 60,
    data_quality: float = 1.0,
    signal_id: str = "test",
) -> Signal:
    return Signal(
        signal_id=signal_id,
        signal_type=SignalType.TREND_BREAK,
        classification=Classification.COMMERCIAL,
        entity="Test",
        metric="metric",
        metric_family="revenue",
        detected_at=dt.date(2025, 6, 1),
        direction="increase",
        baseline_value=1.0,
        current_value=2.0,
        magnitude=magnitude,
        persistence_days=persistence_days,
        data_quality_score=data_quality,
        summary="test signal",
    )


class TestStrength:
    def test_saturates_at_the_configured_move(self):
        assert strength_score([make_signal(magnitude=0.5)]) == pytest.approx(1.0)
        assert strength_score([make_signal(magnitude=5.0)]) == pytest.approx(1.0)

    def test_scales_below_saturation(self):
        assert strength_score([make_signal(magnitude=0.25)]) == pytest.approx(0.5)

    def test_uses_largest_not_average(self):
        """A decisive signal should not be diluted by a marginal one alongside it."""
        signals = [make_signal(magnitude=0.5, signal_id="a"), make_signal(magnitude=0.05, signal_id="b")]
        assert strength_score(signals) == pytest.approx(1.0)

    def test_direction_does_not_matter(self):
        assert strength_score([make_signal(magnitude=-0.5)]) == pytest.approx(1.0)

    def test_no_signals(self):
        assert strength_score([]) == 0.0


class TestPersistence:
    def test_saturates_at_four_weeks(self):
        assert persistence_score([make_signal(persistence_days=28)]) == pytest.approx(1.0)
        assert persistence_score([make_signal(persistence_days=200)]) == pytest.approx(1.0)

    def test_short_lived_signal_scores_low(self):
        assert persistence_score([make_signal(persistence_days=7)]) == pytest.approx(0.25)

    def test_uses_longest_running(self):
        signals = [make_signal(persistence_days=28, signal_id="a"), make_signal(persistence_days=1, signal_id="b")]
        assert persistence_score(signals) == pytest.approx(1.0)


class TestCorroboration:
    def test_all_checks_passing(self):
        assert corroboration_score({"a": True, "b": True}) == 1.0

    def test_partial(self):
        assert corroboration_score({"a": True, "b": False, "c": True}) == pytest.approx(2 / 3)

    def test_no_checks_is_neutral_not_free(self):
        """Seeking no corroboration is neither evidence for nor against."""
        assert corroboration_score({}) == 0.5

    def test_all_failing(self):
        assert corroboration_score({"a": False}) == 0.0


class TestDataQuality:
    def test_uses_worst_input_not_average(self):
        """Good data must not launder bad data."""
        signals = [
            make_signal(data_quality=1.0, signal_id="clean"),
            make_signal(data_quality=0.0, signal_id="broken"),
        ]
        assert data_quality_score(signals) == 0.0

    def test_single_signal(self):
        assert data_quality_score([make_signal(data_quality=0.7)]) == pytest.approx(0.7)


class TestScoreConfidence:
    def test_perfect_signal_scores_one(self):
        result = score_confidence([make_signal()], {"check": True})
        assert result.overall == pytest.approx(1.0)

    def test_unusable_data_zeroes_confidence_however_strong_the_signal(self):
        """The retention artifact case: enormous, persistent, corroborated, fictional."""
        signal = make_signal(magnitude=10.0, persistence_days=365, data_quality=0.0)
        result = score_confidence([signal], {"a": True, "b": True})
        assert result.evidence_score == pytest.approx(1.0)
        assert result.overall == 0.0

    def test_averaging_would_not_have_caught_that(self):
        """Guards the design choice: had data quality been a fourth weighted
        term instead of a multiplier, this signal would still score highly."""
        signal = make_signal(magnitude=10.0, persistence_days=365, data_quality=0.0)
        result = score_confidence([signal], {"a": True, "b": True})
        naive_average = (1.0 + 1.0 + 1.0 + 0.0) / 4
        assert naive_average > 0.7
        assert result.overall < 0.05

    def test_degraded_data_scales_confidence_proportionally(self):
        result = score_confidence([make_signal(data_quality=0.8)], {"check": True})
        assert result.overall == pytest.approx(0.8)

    def test_notes_record_failed_checks(self):
        result = score_confidence([make_signal()], {"passing": True, "failing": False})
        assert any("failing" in note for note in result.notes)

    def test_notes_flag_low_data_quality(self):
        result = score_confidence([make_signal(data_quality=0.2)], {})
        assert any("Data quality" in note for note in result.notes)

    def test_no_signals_scores_zero(self):
        assert score_confidence([], {"check": True}).overall == 0.0

    def test_explain_is_human_readable(self):
        text = score_confidence([make_signal(data_quality=0.8)], {"check": True}).explain()
        assert "strength" in text and "data quality" in text
