"""Asymmetric loss: valuing a distribution of outcomes the way a business
experiences it rather than the way a spreadsheet averages it.

Expected value treats a pound gained and a pound lost as equal and opposite.
Businesses do not. A loss consumes cash that was earmarked for something else,
it costs the credibility of whoever authorised it, and in the irreversible case
it forecloses the alternative use of that money for months. So a decision whose
upside merely exceeds its downside on average is not automatically worth taking,
and an automated system that approves on expected value alone will, over enough
decisions, take a series of individually defensible bets that collectively hurt.

Two measures are used, and they answer different questions.

`loss_adjusted_value` re-weights the whole distribution, penalising the loss tail
by a multiplier that depends on whether the action can be undone. It answers "is
this worth doing".

`expected_shortfall` looks only at the bad tail and reports the average outcome
inside it. It answers "if this goes wrong, how wrong". The 5th percentile alone
is not enough for that, because it is the boundary of the bad cases rather than
a summary of them, and two actions can share a 5th percentile while one has a
tail that keeps falling well past it.
"""

from __future__ import annotations

import numpy as np


def loss_adjusted_value(samples: np.ndarray, regret_multiplier: float) -> float:
    """Expected value with the loss tail weighted more heavily than the gain tail.

    Equivalent to the expected value when the multiplier is 1.0, so the
    reversible and irreversible cases sit on the same scale and can be compared
    directly against zero.
    """
    values = np.asarray(samples, dtype=float)
    if values.size == 0:
        return 0.0
    gains = values[values > 0].sum()
    losses = values[values <= 0].sum()
    return float((gains + regret_multiplier * losses) / values.size)


def expected_shortfall(samples: np.ndarray, tail: float = 0.05) -> float:
    """Mean outcome within the worst `tail` share of draws.

    Returns a signed value in the same units as the samples, so a negative
    result is a loss. If the whole tail is profitable, the result is positive
    and simply means the action has no modelled downside at this quantile.
    """
    values = np.asarray(samples, dtype=float)
    if values.size == 0:
        return 0.0
    cutoff = np.percentile(values, tail * 100)
    worst = values[values <= cutoff]
    if worst.size == 0:
        return float(cutoff)
    return float(worst.mean())


def probability_of_loss_worse_than(samples: np.ndarray, threshold: float) -> float:
    """Share of draws in which the loss exceeds `threshold` (a positive number).

    Phrased as a loss rather than a return because that is the question the
    autonomy gate is asking: not how often this wins, but how often it loses
    more than the business is willing to absorb.
    """
    values = np.asarray(samples, dtype=float)
    if values.size == 0:
        return 0.0
    return float(np.mean(values < -abs(threshold)))
