"""Tests for the autonomy gate.

What is worth protecting here is not the arithmetic, which is trivial, but the
design decisions that a later simplification would quietly undo. Three in
particular:

  An irreversible action never auto-executes, at any confidence and any
  probability. This is the whole premise, and it is exactly the rule that looks
  redundant once every purchase order in the dataset scores 1.00 on both.

  A 100% modelled probability does not by itself authorise anything, because the
  gate weighs the stressed case as well as the nominal one.

  The stressed run shares the nominal run's seed, so the difference between them
  is attributable to the assumption rather than to sampling noise.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.autonomy.gate import (
    BusinessScale,
    GateCheck,
    Verdict,
    evaluate,
)
from src.autonomy.loss import (
    expected_shortfall,
    loss_adjusted_value,
    probability_of_loss_worse_than,
)
from src.recommendations.recommendation import (
    ActionType,
    ConfidenceBreakdown,
    Recommendation,
    Reversibility,
)
from src.simulation.outcome import summarise

SCALE = BusinessScale(monthly_gross_profit=60_000.0, monthly_paid_spend=20_000.0)


def make_recommendation(
    action_type: ActionType = ActionType.BUDGET_REALLOCATION,
    reversibility: Reversibility = Reversibility.REVERSIBLE,
    confidence: float = 0.90,
    parameters: dict | None = None,
) -> Recommendation:
    return Recommendation(
        recommendation_id="test_rec",
        title="Test",
        action_type=action_type,
        reversibility=reversibility,
        action="Do the thing",
        rationale="Because",
        confidence=ConfidenceBreakdown(
            strength=confidence,
            persistence=confidence,
            corroboration=confidence,
            data_quality=1.0,
            evidence_score=confidence,
            overall=confidence,
        ),
        supporting_signal_ids=["sig"],
        parameters=parameters
        or {
            "monthly_shift_gbp": 500.0,
            "destination_channel": "Google",
            "destination_cac": 15.0,
        },
    )


def make_simulation(samples: np.ndarray, assumptions: dict | None = None):
    return summarise(
        samples=samples,
        recommendation_id="test_rec",
        metric="Incremental gross profit",
        horizon_days=90,
        random_seed=42,
        assumptions=assumptions or {},
    )


class TestAsymmetricLoss:
    def test_a_pure_gain_distribution_is_unaffected_by_the_penalty(self):
        """With nothing in the loss tail the multiplier has nothing to bite on,
        so the loss-adjusted value collapses to the plain expected value. This
        is why the gate cannot rely on it alone."""
        samples = np.full(1000, 100.0)
        assert loss_adjusted_value(samples, 5.0) == pytest.approx(100.0)

    def test_losses_are_weighted_more_heavily_than_gains(self):
        # Symmetric: half the draws win 100, half lose 100. Expected value zero.
        samples = np.concatenate([np.full(500, 100.0), np.full(500, -100.0)])
        assert samples.mean() == pytest.approx(0.0)
        assert loss_adjusted_value(samples, 1.0) == pytest.approx(0.0)
        assert loss_adjusted_value(samples, 3.0) == pytest.approx(-100.0)

    def test_a_positive_expected_value_can_still_fail_the_asymmetric_test(self):
        """The case the multiplier exists for: a bet that wins small and often
        and loses large and rarely. Expected value approves it; a business that
        cannot absorb the tail should not."""
        samples = np.concatenate([np.full(950, 100.0), np.full(50, -1500.0)])
        assert samples.mean() > 0
        assert loss_adjusted_value(samples, 5.0) < 0

    def test_shortfall_summarises_the_tail_rather_than_its_boundary(self):
        """Two distributions can share a 5th percentile while one keeps falling
        well past it, which is the difference the gate needs to see."""
        shallow = np.concatenate([np.full(950, 100.0), np.full(50, -100.0)])
        deep = np.concatenate([np.full(950, 100.0), np.linspace(-100.0, -5000.0, 50)])
        assert np.percentile(shallow, 5) == pytest.approx(np.percentile(deep, 5), rel=0.1)
        assert expected_shortfall(deep) < expected_shortfall(shallow)

    def test_probability_of_loss_ignores_the_sign_of_the_threshold(self):
        samples = np.concatenate([np.full(900, 50.0), np.full(100, -200.0)])
        assert probability_of_loss_worse_than(samples, 100.0) == pytest.approx(0.10)
        assert probability_of_loss_worse_than(samples, -100.0) == pytest.approx(0.10)

    def test_empty_input_does_not_raise(self):
        assert loss_adjusted_value(np.array([]), 2.0) == 0.0
        assert expected_shortfall(np.array([])) == 0.0


class TestReversibilityGoverns:
    def test_a_flawless_irreversible_action_still_routes_to_a_person(self):
        """The central claim of the design. Perfect confidence, every draw
        profitable, no stressed downside, and it still does not auto-execute."""
        recommendation = make_recommendation(
            action_type=ActionType.INVENTORY_PURCHASE,
            reversibility=Reversibility.IRREVERSIBLE,
            confidence=1.0,
            parameters={"total_reorder_cost": 1_000.0, "demand_recently_surged": False},
        )
        samples = np.full(1000, 5_000.0)
        decision = evaluate(
            recommendation, make_simulation(samples), make_simulation(samples), SCALE
        )
        assert decision.verdict is Verdict.PROPOSE
        assert decision.authorised_now_gbp is None
        # But it is marked as routine, so the human knows it needs a signature
        # rather than an argument.
        assert not decision.requires_scrutiny

    def test_an_equivalent_reversible_action_does_auto_execute(self):
        """The comparison that makes the previous test meaningful: identical
        evidence, different consequences of being wrong, different verdict."""
        recommendation = make_recommendation(confidence=1.0)
        samples = np.full(1000, 5_000.0)
        decision = evaluate(
            recommendation, make_simulation(samples), make_simulation(samples), SCALE
        )
        assert decision.verdict is Verdict.AUTO_EXECUTE

    def test_reversibility_is_not_counted_as_a_weakness_in_the_case(self):
        """Every purchase order fails the reversibility check by construction.
        If that counted toward scrutiny, all of them would look contested and
        the flag would stop distinguishing anything."""
        recommendation = make_recommendation(
            action_type=ActionType.INVENTORY_PURCHASE,
            reversibility=Reversibility.IRREVERSIBLE,
            confidence=1.0,
            parameters={"total_reorder_cost": 1_000.0},
        )
        samples = np.full(1000, 5_000.0)
        decision = evaluate(
            recommendation, make_simulation(samples), make_simulation(samples), SCALE
        )
        check = next(c for c in decision.checks if c.name == "action_is_reversible")
        assert not check.passed
        # Flagged as routing rather than special-cased by name, so anything
        # presenting these checks can tell "this is a purchase order" apart from
        # "this costs more than we tolerate" without knowing the check names.
        assert check.routing
        assert not decision.requires_scrutiny

    def test_only_the_reversibility_check_routes(self):
        """Routing is a narrow exemption. If a second check ever claimed it, a
        real objection would stop counting toward scrutiny without anyone
        noticing."""
        recommendation = make_recommendation(
            action_type=ActionType.INVENTORY_PURCHASE,
            reversibility=Reversibility.IRREVERSIBLE,
            confidence=1.0,
            parameters={"total_reorder_cost": 1_000.0},
        )
        samples = np.full(1000, 5_000.0)
        decision = evaluate(
            recommendation, make_simulation(samples), make_simulation(samples), SCALE
        )
        assert [c.name for c in decision.checks if c.routing] == ["action_is_reversible"]


class TestStressGovernsAutonomy:
    def test_a_certain_looking_action_is_blocked_by_its_stressed_case(self):
        """Nominal says every draw wins. The stress test says the bad case costs
        more than the business can absorb. The gate follows the stress test."""
        recommendation = make_recommendation(confidence=1.0)
        nominal = make_simulation(np.full(1000, 5_000.0))
        stressed = make_simulation(np.full(1000, -20_000.0))
        decision = evaluate(recommendation, nominal, stressed, SCALE)
        assert nominal.prob_positive == 1.0
        assert decision.verdict is Verdict.PROPOSE
        assert "stressed_case_is_affordable" in {c.name for c in decision.failed_checks}

    def test_capital_left_in_unsold_stock_counts_as_money_at_risk(self):
        """A purchase can be profitable on paper while leaving most of its cash
        sitting in stock nobody bought. The profit distribution alone cannot see
        that, so the gate reads the capital figure too."""
        recommendation = make_recommendation(
            action_type=ActionType.INVENTORY_PURCHASE,
            reversibility=Reversibility.IRREVERSIBLE,
            confidence=1.0,
            parameters={"total_reorder_cost": 10_000.0},
        )
        samples = np.full(1000, 2_000.0)
        stressed = make_simulation(
            samples, assumptions={"capital_unsold_at_horizon_p95": 8_000.0}
        )
        decision = evaluate(recommendation, make_simulation(samples), stressed, SCALE)
        assert decision.worst_case_cost_gbp == pytest.approx(8_000.0)
        assert decision.requires_scrutiny

    def test_confidence_below_the_bar_blocks_a_reversible_action(self):
        recommendation = make_recommendation(confidence=0.50)
        samples = np.full(1000, 5_000.0)
        decision = evaluate(
            recommendation, make_simulation(samples), make_simulation(samples), SCALE
        )
        assert decision.verdict is Verdict.PROPOSE
        assert "confidence_meets_bar" in {c.name for c in decision.failed_checks}

    def test_the_irreversible_bar_is_higher_than_the_reversible_one(self):
        """0.80 clears the reversible bar and misses the irreversible one, so
        the same evidence yields different answers depending on the stakes."""
        samples = np.full(1000, 5_000.0)
        reversible = evaluate(
            make_recommendation(confidence=0.80),
            make_simulation(samples),
            make_simulation(samples),
            SCALE,
        )
        irreversible = evaluate(
            make_recommendation(
                action_type=ActionType.INVENTORY_PURCHASE,
                reversibility=Reversibility.IRREVERSIBLE,
                confidence=0.80,
                parameters={"total_reorder_cost": 1_000.0},
            ),
            make_simulation(samples),
            make_simulation(samples),
            SCALE,
        )
        assert not reversible.requires_scrutiny
        assert "confidence_meets_bar" in {c.name for c in irreversible.failed_checks}


class TestStepSizing:
    def test_a_large_reversible_action_is_tranched_rather_than_refused(self):
        """Splitting the move is the mechanism that lets the engine act at all.
        Refusing it outright would push every material decision to a human and
        make the gate a formality."""
        recommendation = make_recommendation(
            confidence=1.0,
            parameters={
                "monthly_shift_gbp": 5_000.0,
                "destination_channel": "Google",
                "destination_cac": 15.0,
            },
        )
        samples = np.full(1000, 5_000.0)
        decision = evaluate(
            recommendation, make_simulation(samples), make_simulation(samples), SCALE
        )
        cap = SCALE.monthly_paid_spend * 0.05
        assert decision.verdict is Verdict.AUTO_EXECUTE
        assert decision.exposure_gbp == pytest.approx(5_000.0)
        assert decision.authorised_now_gbp == pytest.approx(cap)

    def test_step_sizing_does_not_block_the_verdict(self):
        recommendation = make_recommendation(
            confidence=1.0,
            parameters={
                "monthly_shift_gbp": 5_000.0,
                "destination_channel": "Google",
                "destination_cac": 15.0,
            },
        )
        samples = np.full(1000, 5_000.0)
        decision = evaluate(
            recommendation, make_simulation(samples), make_simulation(samples), SCALE
        )
        step = next(c for c in decision.checks if c.name == "within_autonomous_step_size")
        assert not step.passed
        assert not step.blocking
        assert not decision.requires_scrutiny

    def test_step_sizing_is_omitted_where_the_engine_cannot_act_anyway(self):
        """A media-spend cap has nothing to say about a purchase order, and
        printing an authorised amount under a verdict that forbids acting would
        be actively misleading."""
        recommendation = make_recommendation(
            action_type=ActionType.INVENTORY_PURCHASE,
            reversibility=Reversibility.IRREVERSIBLE,
            confidence=1.0,
            parameters={"total_reorder_cost": 50_000.0},
        )
        samples = np.full(1000, 5_000.0)
        decision = evaluate(
            recommendation, make_simulation(samples), make_simulation(samples), SCALE
        )
        assert "within_autonomous_step_size" not in {c.name for c in decision.checks}


class TestRoutingWithoutACashModel:
    def test_an_investigation_escalates_rather_than_being_scored(self):
        """Its payoff is information. Assigning it a cash value would mean
        inventing the number the gate then tests against."""
        recommendation = make_recommendation(action_type=ActionType.INVESTIGATION)
        decision = evaluate(recommendation, None, None, SCALE)
        assert decision.verdict is Verdict.ESCALATE
        assert decision.checks == []

    def test_a_deliberate_no_action_is_recorded_as_a_decision(self):
        """Suppression is a verdict, not an absence of one. A considered no and
        an oversight look identical from outside unless the no is written down."""
        recommendation = make_recommendation(action_type=ActionType.NO_ACTION)
        decision = evaluate(recommendation, None, None, SCALE)
        assert decision.verdict is Verdict.SUPPRESS

    def test_every_verdict_carries_a_safeguard(self):
        """Whatever the gate decides, something has to make the decision safe:
        a rollback trigger, a pre-commitment check, or a review date."""
        for action_type, reversibility in [
            (ActionType.BUDGET_REALLOCATION, Reversibility.REVERSIBLE),
            (ActionType.INVENTORY_PURCHASE, Reversibility.IRREVERSIBLE),
            (ActionType.INVESTIGATION, Reversibility.REVERSIBLE),
            (ActionType.FLOW_OPTIMISATION, Reversibility.REVERSIBLE),
            (ActionType.NO_ACTION, Reversibility.REVERSIBLE),
        ]:
            recommendation = make_recommendation(
                action_type=action_type,
                reversibility=reversibility,
                parameters={"total_reorder_cost": 100.0, "monthly_shift_gbp": 100.0,
                            "destination_cac": 10.0, "destination_channel": "Google"},
            )
            decision = evaluate(recommendation, None, None, SCALE)
            assert decision.safeguard, f"{action_type} has no safeguard"


class TestStressSharesTheNominalSeed:
    def test_the_stressed_run_draws_the_same_random_numbers(self, monkeypatch):
        """Otherwise the difference between the two runs would mix the effect of
        the assumption with the effect of different noise, and the stress test
        would stop being a measurement of anything.
        """
        import src.simulation as simulation

        captured: dict[bool, float] = {}

        def recorder(recommendation, con, rng, stress=False):
            captured[stress] = float(rng.random())
            return None

        monkeypatch.setitem(
            simulation.SIMULATORS, ActionType.BUDGET_REALLOCATION, recorder
        )
        recommendation = make_recommendation()
        simulation.simulate(recommendation, con=None)
        simulation.simulate(recommendation, con=None, stress=True)

        assert captured[False] == captured[True]

    def test_different_recommendations_do_not_share_draws(self, monkeypatch):
        """Each recommendation is seeded from its own id, so adding or removing
        one does not silently shift everyone else's numbers."""
        import src.simulation as simulation

        captured: list[float] = []

        def recorder(recommendation, con, rng, stress=False):
            captured.append(float(rng.random()))
            return None

        monkeypatch.setitem(
            simulation.SIMULATORS, ActionType.BUDGET_REALLOCATION, recorder
        )
        first = make_recommendation()
        second = make_recommendation()
        object.__setattr__(second, "recommendation_id", "a_different_id")
        simulation.simulate(first, con=None)
        simulation.simulate(second, con=None)

        assert captured[0] != captured[1]


class TestGateCheckDefaults:
    def test_checks_block_unless_explicitly_marked_otherwise(self):
        """Defaulting to blocking means a new check added later restricts
        autonomy until someone decides it should not, rather than silently
        widening it."""
        assert GateCheck(name="x", passed=False, detail="").blocking
