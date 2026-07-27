"""Outcome simulation: recommendations in, outcome distributions out."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.config import SIMULATION
from src.recommendations.recommendation import Recommendation
from src.simulation.models import NOT_SIMULATED_REASON, SIMULATORS
from src.simulation.outcome import SimulationResult, summarise

__all__ = [
    "NOT_SIMULATED_REASON",
    "SimulationResult",
    "simulate",
    "simulate_all",
    "simulations_to_frame",
    "summarise",
]


def simulate(
    recommendation: Recommendation,
    con,
    seed: int | None = None,
    stress: bool = False,
) -> SimulationResult | None:
    """Simulate one recommendation, or return None if it has no cash outcome.

    Each recommendation gets its own generator, seeded from the global seed
    plus a hash of its id. That keeps every result reproducible while making
    them independent of one another, so adding or removing a recommendation
    does not silently change the numbers for all the others.

    `stress` re-runs the same model with its most fragile assumptions set
    against the recommendation. The seed is deliberately unchanged, so the
    stressed and nominal runs share their random draws and any difference
    between them is attributable to the assumption rather than to noise.
    """
    simulator = SIMULATORS.get(recommendation.action_type)
    if simulator is None:
        return None

    base_seed = SIMULATION["random_seed"] if seed is None else seed
    offset = int.from_bytes(recommendation.recommendation_id.encode()[:4].ljust(4, b"\0"), "little")
    rng = np.random.default_rng(base_seed + offset % 100_000)
    return simulator(recommendation, con, rng, stress=stress)


def simulate_all(
    recommendations: list[Recommendation], con, seed: int | None = None, stress: bool = False
) -> dict[str, SimulationResult]:
    results: dict[str, SimulationResult] = {}
    for recommendation in recommendations:
        result = simulate(recommendation, con, seed, stress=stress)
        if result is not None:
            results[recommendation.recommendation_id] = result
    return results


def simulations_to_frame(results: dict[str, SimulationResult]) -> pd.DataFrame:
    if not results:
        return pd.DataFrame()
    return pd.DataFrame([r.to_row() for r in results.values()])
