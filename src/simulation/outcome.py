"""The result of simulating a recommendation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True)
class SimulationResult:
    """A distribution of outcomes, summarised.

    Reports an interval and a probability rather than a single expected value.
    A point estimate invites a decision to be made as though the number were
    known, and the entire reason for simulating is that it is not.
    """

    recommendation_id: str
    metric: str
    horizon_days: int
    n_draws: int
    random_seed: int

    mean: float
    p5: float
    p50: float
    p95: float
    prob_positive: float

    assumptions: dict[str, Any] = field(default_factory=dict)
    """Every input that was assumed rather than measured, so a reader can see
    what the answer is resting on."""

    samples: np.ndarray | None = field(default=None, repr=False, compare=False)

    @property
    def downside(self) -> float:
        """The 5th percentile: roughly the bad case, though not the worst one."""
        return self.p5

    def summary(self) -> str:
        return (
            f"{self.metric} over {self.horizon_days} days: "
            f"central estimate GBP {self.p50:,.0f}, "
            f"90% credible interval GBP {self.p5:,.0f} to GBP {self.p95:,.0f}, "
            f"probability of a positive return {self.prob_positive:.0%}"
        )

    def to_row(self) -> dict:
        return {
            "recommendation_id": self.recommendation_id,
            "horizon_days": self.horizon_days,
            "p5": round(self.p5, 0),
            "p50": round(self.p50, 0),
            "p95": round(self.p95, 0),
            "mean": round(self.mean, 0),
            "prob_positive": round(self.prob_positive, 3),
        }


def summarise(
    samples: np.ndarray,
    recommendation_id: str,
    metric: str,
    horizon_days: int,
    random_seed: int,
    assumptions: dict[str, Any],
    credible_interval: float = 0.90,
) -> SimulationResult:
    tail = (1 - credible_interval) / 2 * 100
    return SimulationResult(
        recommendation_id=recommendation_id,
        metric=metric,
        horizon_days=horizon_days,
        n_draws=int(samples.size),
        random_seed=random_seed,
        mean=float(np.mean(samples)),
        p5=float(np.percentile(samples, tail)),
        p50=float(np.percentile(samples, 50)),
        p95=float(np.percentile(samples, 100 - tail)),
        prob_positive=float(np.mean(samples > 0)),
        assumptions=assumptions,
        samples=samples,
    )
