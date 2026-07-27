"""Statistical primitives shared by the detectors.

Kept separate from the detectors themselves so they can be tested against
series with known, injected changepoints, which is the only honest way to claim
a detector works.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats

# MAD scaled by this constant estimates the standard deviation of a normal
# distribution, so a robust z-score stays comparable to a conventional one.
_MAD_TO_SIGMA = 1.4826

# Ceiling on how much any single observation can contribute to the CUSUM, in
# baseline standard deviations. Three sigma is generous for a genuine shift
# (which persists and so accumulates anyway) while preventing one freak day
# from single-handedly raising an alarm.
_CUSUM_CLIP = 3.0


def robust_zscore(values: np.ndarray | pd.Series) -> np.ndarray:
    """Z-score using median and MAD instead of mean and standard deviation.

    The conventional z-score has a circularity problem for anomaly detection:
    a large outlier inflates the standard deviation it is being measured
    against, so the very spikes worth catching partly hide themselves. Median
    and MAD are unaffected by a minority of extreme points.

    Returns zeros when the series has no dispersion, rather than dividing by
    zero and producing infinities that later comparisons would silently treat
    as significant.
    """
    arr = np.asarray(values, dtype=float)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return np.zeros_like(arr)

    median = np.median(finite)
    mad = np.median(np.abs(finite - median))
    scale = mad * _MAD_TO_SIGMA

    if scale <= 0:
        # Degenerate spread: fall back to the standard deviation, and if that
        # is also zero the series is constant and nothing is anomalous.
        scale = float(np.std(finite))
        if scale <= 0:
            return np.zeros_like(arr)

    return (arr - median) / scale


def cusum_changepoint(
    values: np.ndarray | pd.Series,
    baseline_window: int,
    drift: float = 0.5,
    threshold: float = 5.0,
) -> int | None:
    """Locate the first sustained level shift using a two-sided CUSUM.

    Returns the *detection* index, which lags the change itself by however many
    observations the evidence took to accumulate. Callers that need the change's
    origin, which is most of them, should use ``cusum_changepoint_span``: using
    the detection index as a before/after boundary quietly counts the first
    post-change days as "before", inflating the baseline with exactly the values
    it is supposed to exclude.
    """
    span = cusum_changepoint_span(values, baseline_window, drift, threshold)
    return None if span is None else span[1]


def cusum_changepoint_span(
    values: np.ndarray | pd.Series,
    baseline_window: int,
    drift: float = 0.5,
    threshold: float = 5.0,
) -> tuple[int, int] | None:
    """Locate the first sustained level shift, as (origin, detection) indices.

    CUSUM accumulates small deviations from a baseline mean, so it fires on a
    persistent modest shift that a per-point threshold would never catch, while
    ignoring a single large spike that reverts. That is the right tradeoff
    here: a CPC that rises 15% and stays there matters far more than one day at
    triple price.

    The ``drift`` term is the per-observation allowance, in baseline standard
    deviations, subtracted before accumulating. It is what stops random noise
    accumulating into an alarm given enough time.

    ``origin`` is where the breaching side's statistic last left zero, which is
    the standard changepoint estimate for a CUSUM: the accumulating run that
    ended in the alarm began there, so it is the best available date for when
    the shift started. ``detection`` is where the threshold was crossed.
    Returns None if the series never shifts.
    """
    arr = np.asarray(values, dtype=float)
    if arr.size <= baseline_window:
        return None

    baseline = arr[:baseline_window]
    baseline = baseline[np.isfinite(baseline)]
    if baseline.size < 2:
        return None

    mean = float(np.mean(baseline))
    sigma = float(np.std(baseline, ddof=1))
    if sigma <= 0:
        return None

    upper = lower = 0.0
    upper_start = lower_start = None
    for i in range(baseline_window, arr.size):
        if not np.isfinite(arr[i]):
            continue
        # Clip each observation's contribution. Without this a single extreme
        # value can exceed the threshold on its own and be reported as a regime
        # change, which is the opposite of what CUSUM is for: a one-day spike
        # that reverts is not a level shift. Clipping forces the alarm to be
        # earned by several consecutive observations.
        standardised = float(np.clip((arr[i] - mean) / sigma, -_CUSUM_CLIP, _CUSUM_CLIP))

        upper = max(0.0, upper + standardised - drift)
        if upper == 0.0:
            upper_start = None
        elif upper_start is None:
            upper_start = i

        lower = min(0.0, lower + standardised + drift)
        if lower == 0.0:
            lower_start = None
        elif lower_start is None:
            lower_start = i

        if upper > threshold:
            return (upper_start if upper_start is not None else i), i
        if lower < -threshold:
            return (lower_start if lower_start is not None else i), i
    return None


def trailing_consecutive_run(flags: np.ndarray | pd.Series) -> int:
    """Length of the run of True values ending at the last observation.

    Persistence is measured at the end of the series on purpose. A breach that
    happened in October and resolved is history; a breach still running today
    is a decision. Only the latter should drive a recommendation.
    """
    arr = np.asarray(flags, dtype=bool)
    run = 0
    for value in arr[::-1]:
        if not value:
            break
        run += 1
    return run


def sustained_breach(
    flags: np.ndarray | pd.Series,
    window: int = 28,
    min_share: float = 0.5,
    min_run: int = 5,
) -> tuple[int, int | None]:
    """Detect a breach that is sustained but not necessarily unbroken.

    Requiring an unbroken run ending today is too brittle for metrics that sit
    near their threshold. A ratio oscillating either side of break-even is in
    real trouble, but a single good day resets an unbroken-run counter to zero
    and the alert never fires. That is the difference between missing and
    catching a channel whose economics have actually turned.

    A breach is called when, within the trailing ``window``:
      - at least ``min_share`` of observations are in breach, and
      - at least one unbroken run reaches ``min_run``.

    The first condition establishes that the metric genuinely lives below the
    line rather than dipping under it occasionally. The second stops a scatter
    of unrelated bad days adding up to an alarm.

    Returns (days_in_breach_within_window, index_of_first_breach_in_window),
    or (0, None) if not sustained.
    """
    arr = np.asarray(flags, dtype=bool)
    if arr.size == 0:
        return 0, None

    start = max(0, arr.size - window)
    recent = arr[start:]
    breached = int(recent.sum())
    if breached == 0 or breached / recent.size < min_share:
        return 0, None

    longest = current = 0
    for value in recent:
        current = current + 1 if value else 0
        longest = max(longest, current)
    if longest < min_run:
        return 0, None

    first_local = int(np.argmax(recent))
    return breached, start + first_local


def seasonal_factors(dates: pd.Series, values: pd.Series) -> pd.Series:
    """Multiplicative day-of-week factors, estimated robustly.

    This brand trades with a clear weekly rhythm, and without adjustment a
    detector comparing a weekend against a mid-week baseline reports an anomaly
    every Saturday.

    Factors use medians rather than means so that genuine anomalies, which are
    what the detector is hunting for, do not contaminate the seasonal profile
    they are measured against. Factors are estimated over the whole series
    rather than a clean reference period: with only twelve months available, a
    shorter window would produce noisier factors than the bias it avoids.
    """
    frame = pd.DataFrame({"date": pd.to_datetime(dates), "value": values})
    frame = frame[np.isfinite(frame["value"])]
    if frame.empty:
        return pd.Series(dtype=float)

    overall = frame["value"].median()
    if overall == 0:
        return pd.Series(1.0, index=range(7))

    by_dow = frame.groupby(frame["date"].dt.dayofweek)["value"].median() / overall
    # Guard against a day with no data or a zero median producing a factor that
    # would blow up the adjusted series.
    return by_dow.replace(0, np.nan).fillna(1.0)


def deseasonalise(dates: pd.Series, values: pd.Series) -> np.ndarray:
    """Divide out the weekly pattern so level shifts stand out."""
    factors = seasonal_factors(dates, values)
    if factors.empty:
        return np.asarray(values, dtype=float)
    dow = pd.to_datetime(dates).dt.dayofweek
    return np.asarray(values, dtype=float) / dow.map(factors).fillna(1.0).to_numpy()


def compare_periods(before: np.ndarray | pd.Series, after: np.ndarray | pd.Series) -> tuple[float, float]:
    """Effect size and p-value for a shift between two periods.

    Uses Mann-Whitney U rather than a t-test: daily ecommerce metrics are
    skewed and heavy-tailed, and a rank-based test does not assume otherwise.

    The p-value here answers a narrow question, "could this difference plausibly
    be noise", and should not be read as a probability that the recommendation
    is right. It is one of four confidence inputs precisely because on 60-plus
    daily observations almost any real shift produces a tiny p-value, which
    makes it good at rejecting noise and poor at ranking what matters.

    Returns (relative_change, p_value). Relative change is against the before
    median, so it is robust and directly interpretable as a percentage move.
    """
    a = np.asarray(before, dtype=float)
    b = np.asarray(after, dtype=float)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]

    if a.size < 2 or b.size < 2:
        return 0.0, 1.0

    median_before = float(np.median(a))
    median_after = float(np.median(b))
    relative = (median_after - median_before) / abs(median_before) if median_before else 0.0

    try:
        _, p_value = stats.mannwhitneyu(a, b, alternative="two-sided")
    except ValueError:
        # Raised when both samples are identical constants.
        p_value = 1.0

    # Identical constant samples yield a NaN statistic rather than raising.
    # A NaN silently compares False against every threshold, so it would be
    # read downstream as "not significant" by accident rather than by design.
    if not np.isfinite(p_value):
        p_value = 1.0

    return relative, float(p_value)
