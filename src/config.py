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
    "cusum_drift": 0.5,
    "cusum_threshold": 5.0,
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
# Confidence is an explicit weighted blend of four factors rather than an
# opaque number. Weights sum to 1.0.
#
#   strength      how large the move is relative to historical variance
#   persistence   how long it has held
#   data_quality  how much we trust the inputs behind it
#   corroboration whether independent metrics tell the same story
#
# data_quality carries the joint-highest weight on purpose: a huge, persistent,
# well-corroborated move built on broken data is still a bad reason to spend
# money. It is the factor most likely to be wrong in a real pipeline.
CONFIDENCE_WEIGHTS = {
    "strength": 0.30,
    "persistence": 0.20,
    "data_quality": 0.30,
    "corroboration": 0.20,
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
