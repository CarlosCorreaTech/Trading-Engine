"""Central configuration: paths, business constants, and detector thresholds.

Everything that a reviewer might want to challenge lives here rather than being
buried in SQL or detector code. If you disagree with a threshold, this is the
one file you need to change.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SQL_DIR = PROJECT_ROOT / "sql"
WAREHOUSE_PATH = PROJECT_ROOT / "warehouse.duckdb"

# SQL layers execute in this order; files within a layer run in filename order,
# so the numeric prefixes encode intra-layer dependencies.
SQL_LAYERS = ("staging", "core", "semantic", "dq")


# --- Business constants -------------------------------------------------
# The brand is UK-only and every order carries taxes_included = true, so the
# headline total_price is VAT-inclusive. Margin work must use ex-VAT revenue,
# otherwise gross margin is overstated by roughly 17 percentage points.
VAT_RATE = 0.20

# Dataset covers exactly these dates. "Today" for the engine is the last day of
# data, so recommendations are framed as of the end of the observed window.
ANALYSIS_START = "2024-07-01"
ANALYSIS_END = "2025-06-30"

CURRENCY = "GBP"

# The referrer-domain to channel mapping lives in sql/core/02_dim_channel.sql,
# which is its single source of truth. Duplicating it here would let the two
# drift apart silently.
#
# Only these two channels have a spend file, so only they can have a CAC. TikTok
# sends real traffic with no cost data, and ~27% of orders carry no usable
# referrer, which is why channel CAC is reported as a range rather than a point.
PAID_CHANNELS_WITH_SPEND = ("Meta", "Google")

# Window used for LTV in the CAC:LTV comparison. Short enough that most of the
# dataset's cohorts have matured into it, long enough to capture the repeat
# purchase that justifies acquisition spend.
LTV_WINDOW_DAYS = 60


# --- Detector thresholds ------------------------------------------------
# A signal must both exceed the magnitude bar and persist. Persistence is what
# separates a real regime change from a noisy Tuesday, and it mirrors the
# "5 consecutive days" framing in the brief.
DETECTION = {
    # Trailing window used to establish "normal" before comparing.
    "baseline_window_days": 28,
    # Robust z-score bar (median/MAD based, so outliers don't inflate the
    # baseline and mask the very spike we're looking for).
    "zscore_threshold": 3.0,
    # Consecutive days beyond threshold before a signal fires.
    "min_consecutive_days": 5,
    # CUSUM sensitivity, in units of baseline standard deviation.
    #
    # Tuned empirically against synthetic series with injected changepoints
    # rather than taken from the textbook defaults, which turned out to be
    # unusable here. The classic (drift 0.5, threshold 5.0) produced a 42%
    # false-positive rate on pure noise over a 200-point series: on twelve
    # months of daily data for a dozen SKUs and two channels, that is dozens of
    # invented signals.
    #
    # Measured over 300 trials per configuration (see tests/test_statistics.py,
    # which enforces these numbers so the tuning cannot silently rot):
    #   false positives on stationary noise   0.7%
    #   recall on 15%, 30% and 50% shifts     100%
    #   recall on a trivial 5% shift          22%   (deliberately low)
    #   median detection lag                  ~4 days after the true change
    #
    # The low recall on 5% shifts is a feature. A shift that small is inside
    # normal weekly variation and is not worth a recommendation.
    "cusum_drift": 0.75,
    "cusum_threshold": 8.0,
    # Relative change bars for the domain-specific detectors.
    "cpc_inflation_pct": 0.25,
    "velocity_change_pct": 0.50,
    # A ratio-divergence is called when an upstream engagement metric moves by
    # at least this much while the downstream commercial metric stays within
    # its tolerance. That combination implies measurement, not behaviour.
    "divergence_upstream_pct": 0.15,
    "divergence_downstream_tolerance_pct": 0.10,
    # Inventory cover below this is a stockout risk worth acting on.
    "inventory_cover_days_critical": 45.0,
}


# --- Confidence scoring -------------------------------------------------
# Confidence combines four factors, but not all four the same way.
#
# Three describe how strong the evidence is, and are averaged with these
# weights (which sum to 1.0):
#
#   strength      how large the move is relative to normal variation
#   persistence   how long it has held
#   corroboration whether independent checks agree
#
# The fourth, data quality, multiplies that average rather than joining it.
#
# The distinction matters. Averaging would let a huge, persistent,
# well-corroborated signal built on broken data still score around 0.7, because
# three strong terms outvote one weak one. But those three terms are all
# computed *from* the suspect data, so they are not independent evidence, they
# are the same doubt counted three more times. Multiplying makes data quality a
# ceiling: nothing measured on unusable data can be acted on confidently,
# regardless of how convincing it looks.
#
# This is what mechanically prevents a retention recommendation on this dataset,
# where the retention collapse is enormous, perfectly persistent, and entirely
# fictional.
CONFIDENCE_WEIGHTS = {
    "strength": 0.40,
    "persistence": 0.25,
    "corroboration": 0.35,
}

# A signal must move at least this much to score full marks on strength. Set at
# 50% because a move of that size in a channel cost or a product's velocity is
# unambiguously a regime change rather than drift.
CONFIDENCE_STRENGTH_SATURATION = 0.50

# Days a signal must hold to score full marks on persistence. Four weeks covers
# a full monthly cycle, so a signal surviving it is not a calendar effect.
CONFIDENCE_PERSISTENCE_SATURATION_DAYS = 28


# --- Recommendation rules -----------------------------------------------
# The commercial judgement, kept declarative so it can be argued with without
# reading Python.
RECOMMENDATION_RULES = {
    "budget_reallocation": {
        # Never move more than this share of a channel's budget at once. Paid
        # channels do not respond linearly and a large step destroys the ability
        # to attribute the outcome. Several small moves beat one big one.
        "max_shift_pct_of_source": 0.30,
        # The destination must be demonstrably healthy, not merely healthier
        # than the channel being cut.
        "min_destination_payback": 1.5,
        # Headroom is capped by the most the destination has actually absorbed
        # while staying healthy, rather than assumed to scale indefinitely.
        # If that evidence comes from November or December, it is discounted:
        # Google absorbed 12k in December at stable CPA, but December demand
        # flatters everything, and assuming June behaves like Christmas is how
        # reallocations disappoint.
        "peak_season_headroom_haircut": 0.50,
    },
    "inventory_reorder": {
        # No supplier lead time is given in the dataset. 30 days is a
        # deliberately conservative placeholder for imported supplements and is
        # flagged as an assumption on every recommendation it affects, because
        # it directly sets the reorder quantity.
        "assumed_supplier_lead_time_days": 30,
        "safety_stock_days": 14,
        # Order enough to cover this long after delivery. A quarter balances
        # holding cost against reorder frequency.
        "target_cover_days": 90,
    },
}


# --- Simulation ---------------------------------------------------------
SIMULATION = {
    "n_draws": 10_000,
    "random_seed": 42,
    # Trailing window bootstrapped for input distributions. Long enough for a
    # stable empirical distribution, short enough to reflect current conditions.
    "bootstrap_window_days": 90,
    "credible_interval": 0.90,
}


# --- Autonomy gate ------------------------------------------------------
# Reversible actions (shifting ad budget) can be undone within a day and their
# downside is bounded by the amount moved. Irreversible actions (buying stock)
# commit cash, incur holding costs, and cannot be unwound, so they face a much
# higher bar and always route to a human regardless of confidence.
AUTONOMY_GATE = {
    "reversible": {
        "min_confidence": 0.70,
        "min_prob_positive_roi": 0.65,
        "auto_execute_allowed": True,
    },
    "irreversible": {
        "min_confidence": 0.85,
        "min_prob_positive_roi": 0.80,
        "auto_execute_allowed": False,
    },
}
