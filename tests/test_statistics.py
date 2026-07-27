"""Tests for the statistical primitives.

These use synthetic series with known, injected structure. That is the only way
to make a defensible claim about whether a detector works: on the real data
there is no ground truth to check against, so a detector that fires looks
identical to one that hallucinates.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.config import DETECTION
from src.detection.statistics import (
    compare_periods,
    cusum_changepoint,
    deseasonalise,
    robust_zscore,
    seasonal_factors,
    sustained_breach,
    trailing_consecutive_run,
)

SEED = 20240701

# Tests exercise the tuned production parameters, not library defaults, so that
# retuning cannot pass the suite while breaking the detectors.
DRIFT = DETECTION["cusum_drift"]
THRESHOLD = DETECTION["cusum_threshold"]


def _cusum(series, baseline_window: int = 60):
    return cusum_changepoint(series, baseline_window, drift=DRIFT, threshold=THRESHOLD)


def _stationary(n: int = 200, mean: float = 10.0, sd: float = 1.0, seed: int = SEED) -> np.ndarray:
    return np.random.default_rng(seed).normal(mean, sd, n)


def _with_changepoint(
    n: int = 200,
    at: int = 120,
    mean: float = 10.0,
    sd: float = 1.0,
    shift_pct: float = 0.5,
    seed: int = SEED,
) -> np.ndarray:
    series = np.random.default_rng(seed).normal(mean, sd, n)
    series[at:] *= 1 + shift_pct
    return series


class TestRobustZscore:
    def test_constant_series_has_no_anomalies(self):
        assert np.allclose(robust_zscore(np.full(50, 7.0)), 0.0)

    def test_empty_series_is_handled(self):
        assert robust_zscore(np.array([np.nan, np.nan])).shape == (2,)

    def test_single_outlier_does_not_mask_itself(self):
        """The point of using MAD: an extreme value must not inflate the scale
        it is measured against to the point of hiding."""
        series = np.concatenate([_stationary(100), [40.0]])
        robust = robust_zscore(series)
        conventional = (series - series.mean()) / series.std()
        assert robust[-1] > conventional[-1] * 2
        assert robust[-1] > 10

    def test_scale_matches_normal_sigma(self):
        """MAD scaled by 1.4826 should approximate the standard deviation."""
        series = np.random.default_rng(SEED).normal(0.0, 5.0, 20_000)
        assert robust_zscore(series).std() == pytest.approx(1.0, abs=0.05)


class TestCusumChangepoint:
    def test_finds_injected_shift_close_to_truth(self):
        detected = _cusum(_with_changepoint(at=120))
        assert detected is not None
        assert 120 <= detected <= 140, "should fire shortly after the true changepoint"

    def test_does_not_fire_on_stationary_noise(self):
        assert _cusum(_stationary()) is None

    def test_detects_downward_shift(self):
        detected = _cusum(_with_changepoint(at=120, shift_pct=-0.4))
        assert detected is not None and detected >= 120

    def test_ignores_a_single_reverting_spike(self):
        """A one-day spike is not a regime change and must not be reported as one."""
        series = _stationary()
        series[150] = 60.0
        assert _cusum(series) is None

    def test_returns_none_when_series_too_short(self):
        assert _cusum(np.arange(10.0)) is None

    def test_returns_none_for_zero_variance_baseline(self):
        series = np.concatenate([np.full(60, 5.0), np.full(60, 9.0)])
        assert _cusum(series) is None


class TestCusumAccuracy:
    """Precision and recall over many synthetic series, rather than one example."""

    N_SERIES = 100

    def test_recall_on_real_shifts(self):
        detected = sum(
            _cusum(_with_changepoint(at=120, shift_pct=0.30, seed=SEED + i))
            is not None
            for i in range(self.N_SERIES)
        )
        recall = detected / self.N_SERIES
        assert recall >= 0.95, f"recall {recall:.2f} too low on 30% level shifts"

    def test_false_positive_rate_on_stationary_series(self):
        false_alarms = sum(
            _cusum(_stationary(seed=SEED + i)) is not None
            for i in range(self.N_SERIES)
        )
        rate = false_alarms / self.N_SERIES
        assert rate <= 0.05, f"false positive rate {rate:.2f} too high on pure noise"

    def test_small_shifts_are_not_over_detected(self):
        """A 5% drift should mostly stay below the bar; firing on everything is
        as useless as firing on nothing."""
        detected = sum(
            _cusum(_with_changepoint(at=120, shift_pct=0.05, seed=SEED + i))
            is not None
            for i in range(self.N_SERIES)
        )
        assert detected / self.N_SERIES <= 0.35


class TestSustainedBreach:
    def test_detects_oscillating_breach(self):
        """The Meta payback case: mostly below the line, with recoveries."""
        flags = np.array([False] * 60 + [True] * 8 + [False] * 2 + [True] * 12 + [False])
        days, start = sustained_breach(flags, window=28, min_share=0.5, min_run=5)
        assert days >= 15 and start is not None

    def test_ignores_scattered_bad_days(self):
        flags = np.zeros(100, dtype=bool)
        flags[[70, 75, 80, 85, 90, 95]] = True
        assert sustained_breach(flags, window=28)[0] == 0

    def test_ignores_resolved_breach(self):
        """A breach that ended long ago is history, not a decision."""
        flags = np.array([True] * 30 + [False] * 60)
        assert sustained_breach(flags, window=28)[0] == 0

    def test_requires_minimum_run_even_when_dense(self):
        flags = np.zeros(100, dtype=bool)
        flags[72::2] = True  # every other day: dense but never sustained
        assert sustained_breach(flags, window=28, min_run=5)[0] == 0

    def test_empty_input(self):
        assert sustained_breach(np.array([], dtype=bool)) == (0, None)


class TestTrailingConsecutiveRun:
    def test_counts_only_the_trailing_run(self):
        assert trailing_consecutive_run(np.array([True] * 5 + [False] + [True] * 3)) == 3

    def test_zero_when_last_is_false(self):
        assert trailing_consecutive_run(np.array([True] * 10 + [False])) == 0

    def test_all_true(self):
        assert trailing_consecutive_run(np.array([True] * 7)) == 7


class TestSeasonality:
    def _weekly_series(self, n: int = 364) -> tuple[pd.Series, pd.Series]:
        dates = pd.date_range("2024-07-01", periods=n, freq="D")
        weekend_boost = np.where(dates.dayofweek >= 5, 1.5, 1.0)
        values = 100 * weekend_boost + np.random.default_rng(SEED).normal(0, 2, n)
        return pd.Series(dates), pd.Series(values)

    def test_factors_recover_the_weekly_pattern(self):
        dates, values = self._weekly_series()
        factors = seasonal_factors(dates, values)
        assert factors[5] == pytest.approx(1.5, rel=0.1)
        assert factors[0] == pytest.approx(1.0, rel=0.1)

    def test_deseasonalising_flattens_the_week(self):
        dates, values = self._weekly_series()
        adjusted = pd.Series(deseasonalise(dates, values))
        spread_before = values.groupby(dates.dt.dayofweek).mean().std()
        spread_after = adjusted.groupby(dates.dt.dayofweek).mean().std()
        assert spread_after < spread_before * 0.2

    def test_seasonality_alone_does_not_trigger_a_changepoint(self):
        """Without adjustment a weekly cycle can look like a level shift."""
        dates, values = self._weekly_series()
        adjusted = deseasonalise(dates, values)
        assert _cusum(adjusted) is None


class TestComparePeriods:
    def test_detects_a_real_difference(self):
        before = _stationary(100, mean=10.0)
        after = _stationary(100, mean=15.0, seed=SEED + 1)
        relative, p_value = compare_periods(before, after)
        assert relative == pytest.approx(0.5, abs=0.15)
        assert p_value < 0.01

    def test_no_difference_gives_high_p_value(self):
        relative, p_value = compare_periods(
            _stationary(100, seed=SEED), _stationary(100, seed=SEED + 99)
        )
        assert abs(relative) < 0.15
        assert p_value > 0.05

    def test_handles_degenerate_input(self):
        assert compare_periods(np.array([1.0]), np.array([2.0])) == (0.0, 1.0)

    def test_identical_constants_are_not_significant(self):
        relative, p_value = compare_periods(np.full(20, 3.0), np.full(20, 3.0))
        assert relative == 0.0 and p_value == 1.0
