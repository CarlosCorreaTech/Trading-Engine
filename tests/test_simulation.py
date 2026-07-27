"""Tests for the simulation sampling machinery.

The properties worth guarding are the ones that would fail silently: a
bootstrap that quietly destroys autocorrelation still returns plausible-looking
numbers, just with intervals too narrow to be useful, and nothing about the
output would reveal it.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.simulation.monte_carlo import (
    block_bootstrap,
    bootstrap_mean,
    draw_lead_time,
    draw_spend_elasticity,
)
from src.simulation.outcome import summarise

SEED = 20240701


def rng() -> np.random.Generator:
    return np.random.default_rng(SEED)


def autocorrelated_series(n: int = 200, rho: float = 0.8) -> np.ndarray:
    generator = np.random.default_rng(SEED)
    out = np.zeros(n)
    for i in range(1, n):
        out[i] = rho * out[i - 1] + generator.normal(0, 1)
    return out + 10


class TestBlockBootstrap:
    def test_output_shape(self):
        assert block_bootstrap(np.arange(100.0), 50, 30, rng()).shape == (50, 30)

    def test_preserves_the_mean(self):
        series = np.random.default_rng(SEED).normal(10, 2, 200)
        paths = block_bootstrap(series, 2000, 60, rng())
        assert paths.mean() == pytest.approx(series.mean(), rel=0.05)

    def test_only_resamples_observed_values(self):
        series = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
        assert set(np.unique(block_bootstrap(series, 20, 10, rng()))) <= set(series)

    def test_preserves_autocorrelation_better_than_iid_sampling(self):
        """The reason for blocks. Independent day sampling would erase the
        clustering of good and bad weeks and understate the spread of outcomes."""
        series = autocorrelated_series()
        generator = rng()
        blocked = block_bootstrap(series, 500, 60, generator, block_size=7)
        iid = generator.choice(series, size=(500, 60), replace=True)

        def lag1(paths: np.ndarray) -> float:
            centred = paths - paths.mean(axis=1, keepdims=True)
            return float(
                np.mean(np.sum(centred[:, :-1] * centred[:, 1:], axis=1) / np.sum(centred**2, axis=1))
            )

        assert lag1(blocked) > lag1(iid) + 0.2

    def test_block_sampling_widens_the_outcome_spread(self):
        """The practical consequence: narrower intervals from iid sampling
        would make every recommendation look safer than it is."""
        series = autocorrelated_series()
        generator = rng()
        blocked = block_bootstrap(series, 2000, 60, generator, block_size=7).mean(axis=1)
        iid = generator.choice(series, size=(2000, 60), replace=True).mean(axis=1)
        assert blocked.std() > iid.std()

    def test_handles_series_shorter_than_block(self):
        assert block_bootstrap(np.array([1.0, 2.0]), 10, 20, rng()).shape == (10, 20)

    def test_handles_empty_series(self):
        assert np.all(block_bootstrap(np.array([]), 10, 20, rng()) == 0)

    def test_ignores_non_finite_values(self):
        series = np.array([1.0, np.nan, 3.0, np.inf, 5.0, 7.0, 9.0, 11.0, 13.0, 15.0])
        assert np.all(np.isfinite(block_bootstrap(series, 20, 10, rng())))

    def test_is_reproducible_for_a_given_seed(self):
        a = block_bootstrap(np.arange(100.0), 50, 30, np.random.default_rng(7))
        b = block_bootstrap(np.arange(100.0), 50, 30, np.random.default_rng(7))
        assert np.array_equal(a, b)

    def test_differs_across_seeds(self):
        a = block_bootstrap(np.arange(100.0), 50, 30, np.random.default_rng(1))
        b = block_bootstrap(np.arange(100.0), 50, 30, np.random.default_rng(2))
        assert not np.array_equal(a, b)


class TestBootstrapMean:
    def test_centres_on_the_series_mean(self):
        series = np.random.default_rng(SEED).normal(20, 3, 300)
        draws = bootstrap_mean(series, 3000, 90, rng())
        assert np.median(draws) == pytest.approx(series.mean(), rel=0.05)

    def test_longer_horizons_reduce_spread(self):
        """Averaging over more days should narrow the distribution of the mean."""
        series = np.random.default_rng(SEED).normal(20, 3, 300)
        short = bootstrap_mean(series, 2000, 14, rng())
        long = bootstrap_mean(series, 2000, 180, rng())
        assert long.std() < short.std()


class TestPriors:
    def test_elasticity_is_bounded_to_the_economically_possible(self):
        draws = draw_spend_elasticity(10_000, rng())
        assert draws.min() >= 0.0 and draws.max() <= 1.0

    def test_elasticity_centres_on_the_documented_prior(self):
        draws = draw_spend_elasticity(20_000, rng())
        assert draws.mean() == pytest.approx(1 / 3, abs=0.02)

    def test_lead_time_centres_on_the_assumption(self):
        draws = draw_lead_time(30.0, 20_000, rng())
        assert np.median(draws) == pytest.approx(30.0, rel=0.03)

    def test_lead_time_is_skewed_late(self):
        """Deliveries miss late more often than early, so the mean must sit
        above the median. A symmetric prior would understate stockout risk."""
        draws = draw_lead_time(30.0, 20_000, rng())
        assert draws.mean() > np.median(draws)
        assert draws.min() > 0

    def test_lead_time_is_never_negative(self):
        assert draw_lead_time(5.0, 10_000, rng()).min() > 0


class TestSummarise:
    def test_percentiles_and_probability(self):
        samples = np.arange(-500.0, 500.0)
        result = summarise(samples, "test", "profit", 90, SEED, {})
        assert result.p50 == pytest.approx(-0.5, abs=1.0)
        assert result.prob_positive == pytest.approx(0.5, abs=0.01)
        assert result.p5 < result.p50 < result.p95

    def test_all_positive_samples(self):
        result = summarise(np.full(1000, 250.0), "test", "profit", 90, SEED, {})
        assert result.prob_positive == 1.0
        assert result.p5 == result.p95 == 250.0

    def test_credible_interval_width_is_configurable(self):
        samples = np.random.default_rng(SEED).normal(0, 1, 10_000)
        narrow = summarise(samples, "t", "m", 90, SEED, {}, credible_interval=0.50)
        wide = summarise(samples, "t", "m", 90, SEED, {}, credible_interval=0.99)
        assert (wide.p95 - wide.p5) > (narrow.p95 - narrow.p5)

    def test_summary_text_is_readable(self):
        text = summarise(np.full(100, 1000.0), "t", "Incremental gross profit", 90, SEED, {}).summary()
        assert "90 days" in text and "credible interval" in text
