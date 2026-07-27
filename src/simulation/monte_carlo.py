"""Sampling machinery for the outcome simulations.

Inputs are bootstrapped from the brand's own recent history rather than drawn
from fitted parametric distributions. Daily ecommerce series are skewed, spiky
and autocorrelated; a normal distribution fitted to them produces tidy
intervals that understate how bad a bad month can be. Resampling the actual
observed days keeps whatever shape the real data has, including the fat tail.
"""

from __future__ import annotations

import numpy as np


def block_bootstrap(
    series: np.ndarray,
    n_draws: int,
    horizon: int,
    rng: np.random.Generator,
    block_size: int = 7,
) -> np.ndarray:
    """Resample a daily series in contiguous blocks.

    Blocks rather than individual days, because daily trading is
    autocorrelated: quiet weeks cluster, and so do busy ones. Sampling days
    independently would shuffle those runs away and produce simulated periods
    far more average than any real period, which understates the spread of
    outcomes and makes every recommendation look safer than it is.

    A 7-day block also preserves the weekly cycle, so each resampled period
    contains a realistic mix of weekdays and weekends.

    Returns an array of shape (n_draws, horizon).
    """
    clean = np.asarray(series, dtype=float)
    clean = clean[np.isfinite(clean)]
    if clean.size == 0:
        return np.zeros((n_draws, horizon))
    if clean.size <= block_size:
        return rng.choice(clean, size=(n_draws, horizon), replace=True)

    n_blocks = int(np.ceil(horizon / block_size))
    max_start = clean.size - block_size
    starts = rng.integers(0, max_start + 1, size=(n_draws, n_blocks))

    offsets = np.arange(block_size)
    indices = (starts[:, :, None] + offsets[None, None, :]).reshape(n_draws, -1)
    return clean[indices][:, :horizon]


def bootstrap_mean(
    series: np.ndarray,
    n_draws: int,
    horizon: int,
    rng: np.random.Generator,
    block_size: int = 7,
) -> np.ndarray:
    """Distribution of the average value over a future window of `horizon` days."""
    return block_bootstrap(series, n_draws, horizon, rng, block_size).mean(axis=1)


def draw_spend_elasticity(n_draws: int, rng: np.random.Generator) -> np.ndarray:
    """Diminishing returns on incremental ad spend, as an assumed prior.

    This is the single largest assumption in the budget simulation, and it is
    assumed rather than estimated because this dataset cannot identify it.
    Google's monthly spend correlates 0.993 with total order volume: budget is
    set to follow demand. Regressing cost per acquisition on spend therefore
    measures the seasonal demand cycle, not the auction's response to money,
    and duly returns a slope of -0.018, which would imply acquisition gets
    slightly cheaper the more you spend. No amount of additional modelling
    fixes this; it needs a deliberate budget experiment that varies spend
    independently of demand.

    Interpretation: cost per acquisition scales as (1 + spend increase) raised
    to this power. Zero means the channel absorbs extra budget at unchanged
    efficiency. One means the extra budget buys no extra customers at all. The
    bound at one is economically meaningful, since spending more cannot win
    fewer customers.

    Beta(2, 4) puts the mean at 0.33 and keeps meaningful weight out to 0.7,
    which spans the range normally seen when brands do run the experiment. The
    simulation reports how much the answer depends on it.
    """
    return rng.beta(2.0, 4.0, size=n_draws)


def draw_lead_time(
    assumed_days: float, n_draws: int, rng: np.random.Generator
) -> np.ndarray:
    """Supplier lead time, treated as uncertain and skewed late.

    The dataset contains no supplier terms, so the central value is an
    assumption. The shape is not arbitrary though: deliveries arrive late far
    more often than early, so a symmetric distribution would understate
    stockout risk. A lognormal centred on the assumption with a long right tail
    reflects how supply chains actually miss.
    """
    sigma = 0.18
    return rng.lognormal(mean=np.log(assumed_days), sigma=sigma, size=n_draws)
