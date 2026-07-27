"""Runs the pipeline once and shapes the result for the browser.

The whole pipeline takes a few seconds: detect, recommend, simulate twice, then
gate. That is fast enough to run on startup and far too slow to run per request,
so it runs once and the result is held in memory. Nothing here is
request-dependent, because nothing about these decisions depends on who is
asking.

This module deliberately owns all the serialisation. The dataclasses in `src`
describe decisions, and teaching them to describe themselves as JSON for one
particular web page would be putting a presentation concern somewhere it would
later have to be untangled from.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from src.autonomy.gate import BusinessScale, GateDecision, Verdict, evaluate_with_simulations
from src.config import AUTONOMY_GATE, SIMULATION
from src.detection.runner import detect_all
from src.detection.signal import Signal
from src.recommendations.engine import recommend
from src.recommendations.recommendation import Recommendation
from src.simulation import NOT_SIMULATED_REASON
from src.simulation.outcome import SimulationResult
from src.warehouse import connect, query

# Order the verdicts appear in the queue. Not alphabetical and not the enum's
# declaration order: this is descending order of how much attention the reader
# needs to give it, which is what a queue should be sorted by.
VERDICT_ORDER = [
    Verdict.AUTO_EXECUTE,
    Verdict.PROPOSE,
    Verdict.ESCALATE,
    Verdict.SUPPRESS,
]

VERDICT_COPY = {
    Verdict.AUTO_EXECUTE: {
        "label": "Engine acts",
        "blurb": "Reversible, bounded, and monitored. Taken without asking.",
    },
    Verdict.PROPOSE: {
        "label": "Needs approval",
        "blurb": "Prepared and costed. A person authorises the commitment.",
    },
    Verdict.ESCALATE: {
        "label": "Needs a person",
        "blurb": "The payoff is information or judgement, not cash flow.",
    },
    Verdict.SUPPRESS: {
        "label": "Deliberately nothing",
        "blurb": "A considered no, recorded so it is not mistaken for an oversight.",
    },
}

CLASSIFICATION_COPY = {
    "commercial": "The business genuinely changed",
    "data_quality": "The measurement changed, not the business",
    "artifact": "An artifact of how the data was produced",
    "explained": "Real, but accounted for by another signal",
}


@dataclass
class Bundle:
    """Everything the console needs, computed once."""

    overview: dict[str, Any]
    decisions: list[dict[str, Any]]
    signals: list[dict[str, Any]]
    quality: dict[str, Any]
    groups: list[dict[str, Any]] = field(default_factory=list)


_lock = threading.Lock()
_bundle: Bundle | None = None


def get_bundle(refresh: bool = False) -> Bundle:
    global _bundle
    with _lock:
        if _bundle is None or refresh:
            _bundle = _build()
        return _bundle


# --- histogram ------------------------------------------------------------


def _histogram(
    samples: np.ndarray | None, bins: int, lo: float, hi: float
) -> list[int]:
    """Bin counts only.

    Ten thousand floats per scenario is about 200KB of JSON per recommendation,
    almost all of it redundant once it has been drawn as sixty bars. The
    binning happens here so the browser receives what it is going to display.
    """
    if samples is None or samples.size == 0 or not np.isfinite([lo, hi]).all() or hi <= lo:
        return [0] * bins
    counts, _ = np.histogram(np.asarray(samples, dtype=float), bins=bins, range=(lo, hi))
    return [int(c) for c in counts]


def _distribution(
    nominal: SimulationResult | None, stressed: SimulationResult | None, bins: int = 56
) -> dict[str, Any] | None:
    """Both scenarios binned onto one shared axis.

    Shared, because the entire point of the comparison is where the stressed
    distribution sits relative to the nominal one. Two charts with independently
    fitted axes would show two similar-looking humps and hide the finding.
    """
    if nominal is None:
        return None

    pools = [nominal.samples]
    if stressed is not None:
        pools.append(stressed.samples)
    combined = np.concatenate([p for p in pools if p is not None])

    lo = float(min(0.0, np.percentile(combined, 0.2)))
    hi = float(np.percentile(combined, 99.8))
    span = hi - lo
    lo, hi = lo - span * 0.03, hi + span * 0.03

    return {
        "bins": bins,
        "lo": lo,
        "hi": hi,
        "zero_at": (0.0 - lo) / (hi - lo) if hi > lo else 0.0,
        "nominal": {
            "counts": _histogram(nominal.samples, bins, lo, hi),
            "p5": nominal.p5,
            "p50": nominal.p50,
            "p95": nominal.p95,
            "mean": nominal.mean,
            "prob_positive": nominal.prob_positive,
        },
        "stressed": (
            {
                "counts": _histogram(stressed.samples, bins, lo, hi),
                "p5": stressed.p5,
                "p50": stressed.p50,
                "p95": stressed.p95,
                "mean": stressed.mean,
                "prob_positive": stressed.prob_positive,
            }
            if stressed is not None
            else None
        ),
    }


# --- serialisation --------------------------------------------------------


def _serialise_decision(
    recommendation: Recommendation,
    decision: GateDecision,
    nominal: SimulationResult | None,
    stressed: SimulationResult | None,
    signals_by_id: dict[str, Signal],
) -> dict[str, Any]:
    confidence = recommendation.confidence
    return {
        "id": recommendation.recommendation_id,
        "title": recommendation.title,
        "action_type": recommendation.action_type.value,
        "reversibility": recommendation.reversibility.value,
        "verdict": decision.verdict.value,
        "verdict_label": VERDICT_COPY[decision.verdict]["label"],
        "needs_scrutiny": decision.requires_scrutiny,
        "headline": decision.headline,
        "action": recommendation.action,
        "rationale": [p for p in recommendation.rationale.split("\n\n") if p.strip()],
        "caveats": list(recommendation.caveats),
        "safeguard": decision.safeguard,
        "review_after_days": decision.review_after_days,
        "confidence": {
            "overall": confidence.overall,
            "strength": confidence.strength,
            "persistence": confidence.persistence,
            "corroboration": confidence.corroboration,
            "data_quality": confidence.data_quality,
            "evidence_score": confidence.evidence_score,
            "explanation": confidence.explain(),
            "notes": list(confidence.notes),
        },
        "exposure_gbp": decision.exposure_gbp,
        "authorised_now_gbp": decision.authorised_now_gbp,
        "worst_case_cost_gbp": decision.worst_case_cost_gbp,
        "loss_adjusted_value_gbp": decision.loss_adjusted_value_gbp,
        "checks": [
            {
                "name": c.name.replace("_", " "),
                "passed": c.passed,
                "blocking": c.blocking,
                "routing": c.routing,
                "detail": c.detail,
            }
            for c in decision.checks
        ],
        "simulation": (
            {
                "metric": nominal.metric,
                "horizon_days": nominal.horizon_days,
                "n_draws": nominal.n_draws,
                "summary": nominal.summary(),
                "assumptions": {k: _jsonable(v) for k, v in nominal.assumptions.items()},
                "distribution": _distribution(nominal, stressed),
            }
            if nominal is not None
            else {"not_simulated": NOT_SIMULATED_REASON.get(recommendation.action_type, "")}
        ),
        "signals": [
            {
                "id": s.signal_id,
                "classification": s.classification.value,
                "entity": s.entity,
                "metric": s.metric,
                "detected_at": str(s.detected_at),
                "magnitude": s.magnitude,
                "persistence_days": s.persistence_days,
                "data_quality": s.data_quality_score,
                "summary": s.summary,
            }
            for sid in recommendation.supporting_signal_ids
            if (s := signals_by_id.get(sid)) is not None
        ],
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


# --- the run --------------------------------------------------------------


def _build() -> Bundle:
    con = connect()
    try:
        signals = detect_all(con)
        recommendations = recommend(signals, con)
        scale = BusinessScale.from_warehouse(con)
        signals_by_id = {s.signal_id: s for s in signals}

        serialised = [
            _serialise_decision(
                case.recommendation, case.decision, case.nominal, case.stressed, signals_by_id
            )
            for case in evaluate_with_simulations(recommendations, con)
        ]

        return Bundle(
            overview=_overview(con, scale, serialised, signals),
            decisions=serialised,
            groups=_groups(serialised),
            signals=_signals(signals),
            quality=_quality(con),
        )
    finally:
        con.close()


def _overview(
    con, scale: BusinessScale, decisions: list[dict[str, Any]], signals: list[Signal]
) -> dict[str, Any]:
    row = query(
        con,
        """
        SELECT sum(net_revenue)                     AS net_revenue,
               sum(gross_profit)                    AS gross_profit,
               sum(gross_profit) / sum(net_revenue) AS gross_margin,
               sum(orders)                          AS orders,
               sum(new_customers)                   AS new_customers,
               sum(net_revenue) / sum(orders)       AS aov,
               min(date_day)                        AS first_day,
               max(date_day)                        AS last_day
        FROM semantic.daily_business_metrics
        """,
    ).iloc[0]

    checks = query(con, "SELECT passed, is_known_issue FROM dq.check_results")
    committed = sum(d["exposure_gbp"] or 0.0 for d in decisions if d["verdict"] == "propose")
    autonomous = sum(
        d["authorised_now_gbp"] or 0.0 for d in decisions if d["verdict"] == "auto_execute"
    )

    return {
        "window": {"start": str(row.first_day)[:10], "end": str(row.last_day)[:10]},
        "net_revenue": float(row.net_revenue),
        "gross_profit": float(row.gross_profit),
        "gross_margin": float(row.gross_margin),
        "orders": int(row.orders),
        "new_customers": int(row.new_customers),
        "aov": float(row.aov),
        "monthly_gross_profit": scale.monthly_gross_profit,
        "monthly_paid_spend": scale.monthly_paid_spend,
        "checks_total": int(len(checks)),
        "checks_failed": int((~checks.passed).sum()),
        "checks_failed_known": int((~checks.passed & checks.is_known_issue).sum()),
        "signals": len(signals),
        "decisions": len(decisions),
        "awaiting_approval_gbp": committed,
        "authorised_autonomously_gbp": autonomous,
        "n_draws": SIMULATION["n_draws"],
        "gate": {
            "reversible_min_confidence": AUTONOMY_GATE["reversible"]["min_confidence"],
            "irreversible_min_confidence": AUTONOMY_GATE["irreversible"]["min_confidence"],
            "step_cap_pct": AUTONOMY_GATE["max_autonomous_step_pct_of_paid_spend"],
            "loss_tolerance_pct": AUTONOMY_GATE[
                "max_stressed_loss_pct_of_monthly_gross_profit"
            ],
        },
    }


def _groups(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = []
    for verdict in VERDICT_ORDER:
        members = [d["id"] for d in decisions if d["verdict"] == verdict.value]
        if members:
            groups.append(
                {
                    "verdict": verdict.value,
                    "label": VERDICT_COPY[verdict]["label"],
                    "blurb": VERDICT_COPY[verdict]["blurb"],
                    "ids": members,
                }
            )
    return groups


def _signals(signals: list[Signal]) -> list[dict[str, Any]]:
    return [
        {
            "id": s.signal_id,
            "type": s.signal_type.value,
            "classification": s.classification.value,
            "classification_blurb": CLASSIFICATION_COPY.get(s.classification.value, ""),
            "entity": s.entity,
            "metric": s.metric,
            "metric_family": s.metric_family,
            "detected_at": str(s.detected_at),
            "direction": s.direction,
            "magnitude": s.magnitude,
            "persistence_days": s.persistence_days,
            "data_quality": s.data_quality_score,
            "summary": s.summary,
        }
        for s in signals
    ]


def _quality(con) -> dict[str, Any]:
    checks = query(
        con,
        """
        SELECT check_name, check_category, severity, is_known_issue, subject_metric,
               observed_value, threshold, description, passed
        FROM dq.check_results
        ORDER BY passed, is_known_issue DESC, check_name
        """,
    )
    scores = query(
        con, "SELECT metric_family, quality_score FROM dq.metric_quality ORDER BY quality_score"
    )
    return {
        "checks": [
            {
                "name": r.check_name.replace("_", " "),
                "category": r.check_category.replace("_", " "),
                "severity": r.severity,
                "known_issue": bool(r.is_known_issue),
                "metric": r.subject_metric,
                "observed": float(r.observed_value),
                "threshold": float(r.threshold),
                "description": r.description,
                "passed": bool(r.passed),
            }
            for r in checks.itertuples(index=False)
        ],
        "scores": [
            {"family": r.metric_family, "score": float(r.quality_score)}
            for r in scores.itertuples(index=False)
        ],
    }
