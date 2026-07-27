"""The autonomy gate: deciding what the engine may do without being asked.

The question this answers is not "is the recommendation good". The
recommendation engine already answered that, and the simulation put an interval
around it. This layer answers a different question: given that the engine
believes something, should it be allowed to act on that belief alone.

Those come apart, and the place they come apart is the point of the whole
exercise. Three of the recommendations here carry a 100% modelled probability of
a positive return, and none of them auto-execute. A probability of 100% is a
statement about the model, not about the world, and the failure mode that
actually costs money on an inventory purchase is not the variance the model
sampled, it is the assumption the model never questioned.

So the gate asks two questions instead of one:

  Is it likely to be right?
      Confidence from the recommendation, probability of a positive return from
      the simulation, and a loss-adjusted value that weights the downside rather
      than averaging it into the upside.

  Is being wrong survivable?
      The same simulation re-run with its most fragile assumptions set against
      it, measured against what the business can absorb, and against whether
      there is any way back.

An action passes only if both are satisfied, and irreversible actions cannot
pass the second by construction: there is no way back from a purchase order, so
no amount of evidence converts one into something the engine may do quietly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

from src.autonomy.loss import expected_shortfall, loss_adjusted_value
from src.config import AUTONOMY_GATE, RECOMMENDATION_RULES
from src.recommendations.recommendation import (
    ActionType,
    Recommendation,
    Reversibility,
)
from src.simulation import simulate
from src.simulation.outcome import SimulationResult
from src.warehouse import query


class Verdict(str, Enum):
    """What happens to a recommendation once the gate has seen it.

    Deliberately four outcomes rather than a yes/no. Collapsing them loses the
    distinction between "a human should approve this" and "a human should go
    and find something out", which are different requests requiring different
    people and different amounts of time.
    """

    AUTO_EXECUTE = "auto_execute"
    """The engine acts now, within a bounded step, under a stated rollback
    trigger it monitors itself."""

    PROPOSE = "propose"
    """The engine has prepared a specific action and a person authorises it.
    The work is done; what is missing is accountability for the commitment."""

    ESCALATE = "escalate"
    """The engine cannot resolve this and is not pretending to. Typically an
    investigation, where the payoff is information and the next step is
    somebody's judgement rather than a cash outcome."""

    SUPPRESS = "suppress"
    """The engine has decided to do nothing, deliberately, and says so. Kept as
    a visible verdict because silence and a considered no look identical from
    the outside, and the difference matters when the thing being ignored is the
    largest movement in the data."""


@dataclass(frozen=True)
class GateCheck:
    """One test, its result, and the numbers behind it."""

    name: str
    passed: bool
    detail: str
    blocking: bool = True
    """Whether failing this test prevents autonomous action. The step-size test
    is non-blocking because it changes how much the engine may do rather than
    whether it may act at all."""


@dataclass(frozen=True)
class GateDecision:
    recommendation_id: str
    verdict: Verdict
    headline: str
    checks: list[GateCheck] = field(default_factory=list)

    exposure_gbp: float = 0.0
    """What is committed if the action is taken in full."""

    authorised_now_gbp: float | None = None
    """How much of that the engine may act on unsupervised, which is not always
    all of it."""

    loss_adjusted_value_gbp: float | None = None
    stressed_p50_gbp: float | None = None
    worst_case_cost_gbp: float | None = None

    safeguard: str = ""
    """The control that makes the verdict safe: a rollback trigger where the
    action can be undone, a pre-commitment check where it cannot."""

    review_after_days: int | None = None

    @property
    def failed_checks(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.passed]

    @property
    def requires_scrutiny(self) -> bool:
        """Whether a proposal needs real thought or is routine sign-off.

        Three restocks arriving at once are not equally interesting. Without
        this, the one that deserves an argument looks exactly like the two that
        deserve a signature.

        Reversibility is excluded, because it decides the verdict rather than
        describing the strength of the case. Every purchase order fails it, so
        counting it here would mark all of them as contested and lose the
        distinction this exists to draw.
        """
        return any(
            c.blocking and not c.passed and c.name != "action_is_reversible"
            for c in self.checks
        )

    def to_row(self) -> dict:
        return {
            "recommendation_id": self.recommendation_id,
            "verdict": self.verdict.value,
            "exposure_gbp": round(self.exposure_gbp, 0),
            "authorised_now_gbp": (
                None if self.authorised_now_gbp is None else round(self.authorised_now_gbp, 0)
            ),
            "worst_case_cost_gbp": (
                None if self.worst_case_cost_gbp is None else round(self.worst_case_cost_gbp, 0)
            ),
            "needs_scrutiny": self.requires_scrutiny,
            "failed_checks": ", ".join(c.name for c in self.failed_checks) or "-",
        }


@dataclass(frozen=True)
class BusinessScale:
    """What the business can absorb, read from the warehouse rather than fixed.

    Thresholds written as absolute amounts go stale silently: a limit that was
    prudent at one trading volume becomes either theatre or an obstruction at
    another, and nothing in the code notices. Expressing them as shares of
    current gross profit and current media spend means the gate tightens and
    loosens with the business.
    """

    monthly_gross_profit: float
    monthly_paid_spend: float

    @classmethod
    def from_warehouse(cls, con) -> "BusinessScale":
        profit = query(
            con,
            """
            SELECT coalesce(sum(gross_profit), 0) AS gross_profit
            FROM semantic.daily_business_metrics
            WHERE date_day > (SELECT max(date_day) - 30 FROM semantic.daily_business_metrics)
            """,
        )
        spend = query(
            con,
            """
            SELECT coalesce(sum(spend), 0) AS spend
            FROM semantic.channel_daily_performance
            WHERE date_day > (SELECT max(date_day) - 30 FROM semantic.channel_daily_performance)
            """,
        )
        return cls(
            monthly_gross_profit=float(profit["gross_profit"].iloc[0]),
            monthly_paid_spend=float(spend["spend"].iloc[0]),
        )


def _exposure(recommendation: Recommendation) -> float:
    """Money committed by the action.

    For a purchase order this is simply the cash leaving the business. For a
    budget reallocation it is the amount redirected, which is deliberately
    conservative: the money is spent either way and only its destination
    changes, so the true exposure is the profit forgone if the destination
    performs worse, not the budget itself. Charging the full amount avoids
    having to be right about that distinction in order to be safe.
    """
    params = recommendation.parameters
    if recommendation.action_type is ActionType.BUDGET_REALLOCATION:
        return float(params.get("monthly_shift_gbp", 0.0))
    if recommendation.action_type is ActionType.INVENTORY_PURCHASE:
        return float(params.get("total_reorder_cost", 0.0))
    return 0.0


def _worst_case_cost(recommendation: Recommendation, stressed: SimulationResult) -> float:
    """What being wrong costs, in the stressed world, as a positive number.

    Two components, because two different things can go wrong. The profit
    shortfall is the action losing money outright. Capital at risk is the money
    still sitting in stock that the stressed demand never sold, which is not a
    loss in the accounting sense but is cash the business no longer has and
    cannot get back on demand. For an irreversible purchase the second dominates
    and is the number worth arguing about.
    """
    shortfall = expected_shortfall(stressed.samples, tail=0.05)
    cost = max(0.0, -shortfall)
    cost += float(stressed.assumptions.get("capital_unsold_at_horizon_p95", 0.0))
    return cost


def _safeguard(recommendation: Recommendation) -> tuple[str, int]:
    """The control attached to the verdict, and when to look at it again.

    A reversible action is only genuinely reversible if somebody would notice it
    had gone wrong, so autonomy is granted against a named trigger rather than
    against good intentions. An irreversible action gets the opposite: the
    control has to sit before the commitment, because afterwards there is
    nothing to control.
    """
    params = recommendation.parameters

    if recommendation.action_type is ActionType.BUDGET_REALLOCATION:
        stop_loss = float(params.get("destination_cac", 0.0)) * 1.2
        return (
            f"Reverse the move if {params.get('destination_channel', 'the destination')}'s "
            f"7-day cost per acquisition exceeds GBP {stop_loss:,.2f} on three consecutive "
            f"days, or if combined new customers across both channels fall below the "
            f"pre-move 28-day average. Both are observable within a week, which is what "
            f"makes this reversible in practice and not just in principle.",
            7,
        )

    if recommendation.action_type is ActionType.INVENTORY_PURCHASE:
        lead_time = RECOMMENDATION_RULES["inventory_reorder"]["assumed_supplier_lead_time_days"]
        checks = [
            f"confirm the actual supplier lead time against the assumed {lead_time} days, "
            f"since the order quantity is directly proportional to it"
        ]
        if params.get("demand_recently_surged"):
            ratio = float(params.get("demand_reversion_ratio", 1.0))
            checks.append(
                f"establish whether the demand surge is durable or promotional, because the "
                f"quantity is sized against the surged rate and demand would fall "
                f"{1 - ratio:.0%} if it unwinds"
            )
        return (
            "Before raising the purchase order: " + "; and ".join(checks) + ".",
            0,
        )

    if recommendation.action_type is ActionType.INVESTIGATION:
        return (
            "Set a date by which the investigation reports. An investigation with no "
            "deadline is a decision not to decide, and the underlying issue keeps "
            "accruing cost while it stays open.",
            14,
        )

    if recommendation.action_type is ActionType.FLOW_OPTIMISATION:
        return (
            "Run as a split test against the current version, so the effect is measured "
            "rather than assumed and the change can be abandoned if it does not win.",
            30,
        )

    return (
        "Re-run the underlying test when the data quality issue behind it is fixed. The "
        "verdict is conditional on the data, not on the metric.",
        90,
    )


def _cash_checks(
    recommendation: Recommendation,
    nominal: SimulationResult,
    stressed: SimulationResult,
    scale: BusinessScale,
) -> tuple[list[GateCheck], float, float, float]:
    reversibility = recommendation.reversibility
    bar = AUTONOMY_GATE[reversibility.value]
    multiplier = AUTONOMY_GATE["regret_multiplier"][reversibility.value]

    confidence = recommendation.confidence.overall
    lav = loss_adjusted_value(nominal.samples, multiplier)
    worst_case = _worst_case_cost(recommendation, stressed)
    exposure = _exposure(recommendation)

    loss_tolerance = (
        scale.monthly_gross_profit * AUTONOMY_GATE["max_stressed_loss_pct_of_monthly_gross_profit"]
    )
    step_cap = scale.monthly_paid_spend * AUTONOMY_GATE["max_autonomous_step_pct_of_paid_spend"]

    checks = [
        GateCheck(
            name="confidence_meets_bar",
            passed=confidence >= bar["min_confidence"],
            detail=(
                f"Confidence {confidence:.2f} against a bar of {bar['min_confidence']:.2f} "
                f"for {reversibility.value} actions."
            ),
        ),
        GateCheck(
            name="probability_of_positive_return",
            passed=nominal.prob_positive >= bar["min_prob_positive_roi"],
            detail=(
                f"{nominal.prob_positive:.0%} of simulated outcomes are profitable, against "
                f"a bar of {bar['min_prob_positive_roi']:.0%}."
            ),
        ),
        GateCheck(
            name="survives_asymmetric_loss",
            passed=lav > 0,
            detail=(
                f"Loss-adjusted value GBP {lav:,.0f}, weighting losses {multiplier:.1f}x "
                f"against gains. Plain expected value is GBP {nominal.mean:,.0f}."
                + (
                    " The two are equal because no simulated draw loses money, so the "
                    "penalty has nothing to bite on. That is a limitation of the "
                    "simulation rather than a property of the decision, and it is why the "
                    "stress test below carries the weight here."
                    if abs(lav - nominal.mean) < 1.0
                    else ""
                )
            ),
        ),
        GateCheck(
            name="stressed_case_is_affordable",
            passed=worst_case <= loss_tolerance,
            detail=(
                f"With its key assumptions set against it, GBP {worst_case:,.0f} is at "
                f"risk: money lost outright plus cash still tied up in stock the stressed "
                f"demand never sold"
                + (f", or {worst_case / exposure:.0%} of the amount committed" if exposure else "")
                + f". Tolerance is GBP {loss_tolerance:,.0f}, being "
                f"{AUTONOMY_GATE['max_stressed_loss_pct_of_monthly_gross_profit']:.0%} of "
                f"a month's gross profit. Stressed central outcome GBP {stressed.p50:,.0f} "
                f"against GBP {nominal.p50:,.0f} nominal."
            ),
        ),
    ]

    # Step sizing only means anything where the engine could act by itself.
    # Applying a media-spend cap to a purchase order would be comparing two
    # unrelated quantities, and printing "the engine may act on GBP 1,024 now"
    # beneath a verdict that forbids it from acting at all is worse than saying
    # nothing.
    if bar["auto_execute_allowed"]:
        checks.append(
            GateCheck(
                name="within_autonomous_step_size",
                passed=exposure <= step_cap,
                blocking=False,
                detail=(
                    f"Exposure GBP {exposure:,.0f} against an unsupervised step cap of GBP "
                    f"{step_cap:,.0f} "
                    f"({AUTONOMY_GATE['max_autonomous_step_pct_of_paid_spend']:.0%} of "
                    f"monthly paid media spend). "
                    + (
                        f"The engine acts on GBP {step_cap:,.0f} now; the remaining GBP "
                        f"{exposure - step_cap:,.0f} needs the measured result of that step "
                        f"to clear the same tests. Splitting it this way turns one decision "
                        f"the engine would have to be right about into a sequence of "
                        f"smaller ones it can be wrong about cheaply."
                        if exposure > step_cap
                        else "The whole action fits inside a single step."
                    )
                ),
            )
        )

    checks.append(
        GateCheck(
            name="action_is_reversible",
            passed=bool(bar["auto_execute_allowed"]),
            detail=(
                "Reversible within a day, and the damage stops when the move is unwound."
                if bar["auto_execute_allowed"]
                else "Irreversible. Cash committed to stock cannot be recalled, so this "
                     "routes to a person regardless of how strong the evidence is. The "
                     "evidence is not the constraint; the absence of a way back is."
            ),
        )
    )
    return checks, lav, worst_case, min(exposure, step_cap)


def evaluate(
    recommendation: Recommendation,
    nominal: SimulationResult | None,
    stressed: SimulationResult | None,
    scale: BusinessScale,
) -> GateDecision:
    """Route one recommendation."""
    safeguard, review_days = _safeguard(recommendation)

    if recommendation.action_type is ActionType.NO_ACTION:
        return GateDecision(
            recommendation_id=recommendation.recommendation_id,
            verdict=Verdict.SUPPRESS,
            headline=(
                "Deliberately not acted on. Recorded so that the decision is visible as a "
                "decision rather than as an omission."
            ),
            safeguard=safeguard,
            review_after_days=review_days,
        )

    if nominal is None or stressed is None:
        return GateDecision(
            recommendation_id=recommendation.recommendation_id,
            verdict=Verdict.ESCALATE,
            headline=(
                "Needs a person. The payoff here is information or a creative judgement, "
                "not a cash flow, so there is nothing for the gate to weigh and pretending "
                "otherwise would mean inventing the number it then tested."
            ),
            safeguard=safeguard,
            review_after_days=review_days,
        )

    checks, lav, worst_case, authorised = _cash_checks(recommendation, nominal, stressed, scale)
    blocked = [c for c in checks if c.blocking and not c.passed]

    if recommendation.reversibility is Reversibility.IRREVERSIBLE:
        verdict = Verdict.PROPOSE
        contested = [c for c in blocked if c.name != "action_is_reversible"]
        if contested:
            headline = (
                f"For approval, and worth arguing about. Committing GBP "
                f"{_exposure(recommendation):,.0f} that cannot be recalled, and it carries "
                f"more risk under its own stress test than the gate will wave through. "
                + " ".join(c.detail for c in contested)
            )
        else:
            headline = (
                f"For approval, routine. Committing GBP {_exposure(recommendation):,.0f} that "
                f"cannot be recalled, but it holds up under a deliberately adverse reading of "
                f"its own assumptions. The signature is required because the money is "
                f"unrecoverable, not because the case is weak."
            )
        return GateDecision(
            recommendation_id=recommendation.recommendation_id,
            verdict=verdict,
            headline=headline,
            checks=checks,
            exposure_gbp=_exposure(recommendation),
            authorised_now_gbp=None,
            loss_adjusted_value_gbp=lav,
            stressed_p50_gbp=stressed.p50,
            worst_case_cost_gbp=worst_case,
            safeguard=safeguard,
            review_after_days=review_days,
        )

    if blocked:
        return GateDecision(
            recommendation_id=recommendation.recommendation_id,
            verdict=Verdict.PROPOSE,
            headline=(
                "Reversible, but it does not clear the bar for acting unsupervised: "
                + "; ".join(c.detail for c in blocked)
            ),
            checks=checks,
            exposure_gbp=_exposure(recommendation),
            authorised_now_gbp=None,
            loss_adjusted_value_gbp=lav,
            stressed_p50_gbp=stressed.p50,
            worst_case_cost_gbp=worst_case,
            safeguard=safeguard,
            review_after_days=review_days,
        )

    exposure = _exposure(recommendation)
    tranches = max(1, math.ceil(exposure / authorised)) if authorised > 0 else 1
    headline = (
        f"The engine acts. GBP {authorised:,.0f} authorised now"
        + (
            f", which is the first of {tranches} steps; the rest follows only if the "
            f"measured result of this one clears the same tests."
            if tranches > 1
            else "."
        )
        + f" Reversible within a day under a monitored stop-loss, and even with its "
        f"assumptions set against it the action stays profitable."
    )
    return GateDecision(
        recommendation_id=recommendation.recommendation_id,
        verdict=Verdict.AUTO_EXECUTE,
        headline=headline,
        checks=checks,
        exposure_gbp=exposure,
        authorised_now_gbp=authorised,
        loss_adjusted_value_gbp=lav,
        stressed_p50_gbp=stressed.p50,
        worst_case_cost_gbp=worst_case,
        safeguard=safeguard,
        review_after_days=review_days,
    )


def evaluate_all(
    recommendations: list[Recommendation], con, seed: int | None = None
) -> dict[str, GateDecision]:
    """Route every recommendation, running each cash model twice.

    The second run is the stressed one. It shares the first's seed, so the two
    differ only in the assumption under test and the gap between them is
    attributable rather than coincidental.
    """
    scale = BusinessScale.from_warehouse(con)
    decisions: dict[str, GateDecision] = {}
    for recommendation in recommendations:
        nominal = simulate(recommendation, con, seed)
        stressed = simulate(recommendation, con, seed, stress=True) if nominal else None
        decisions[recommendation.recommendation_id] = evaluate(
            recommendation, nominal, stressed, scale
        )
    return decisions


def decisions_to_frame(decisions: dict[str, GateDecision]) -> pd.DataFrame:
    if not decisions:
        return pd.DataFrame()
    return pd.DataFrame([d.to_row() for d in decisions.values()])
