"""Integration tests: the commercial rules against real signals and warehouse.

Two things are under test. That the rules produce the expected recommendations
from this dataset, and -- more importantly -- that their corroborating checks
are genuinely falsifiable: each check must be derived from data that could
have come out the other way, because a check that cannot fail inflates
confidence without adding evidence.
"""

from __future__ import annotations

import datetime as dt
import re

from src.detection.signal import Classification, Signal, SignalType
from src.recommendations.rules import (
    rule_reallocate_paid_budget,
    rule_refresh_declining_flow,
    rule_restock_critical_inventory,
)


def _by_id(recommendations):
    return {r.recommendation_id: r for r in recommendations}


def _stub_signal(signal_id: str, entity: str, magnitude: float, **overrides) -> Signal:
    defaults = dict(
        signal_type=SignalType.TREND_BREAK,
        classification=Classification.COMMERCIAL,
        metric="stub",
        metric_family="channel_efficiency",
        detected_at=dt.date(2025, 4, 1),
        direction="increase" if magnitude > 0 else "decrease",
        baseline_value=1.0,
        current_value=1.0 + magnitude,
        persistence_days=60,
        p_value=None,
        data_quality_score=1.0,
        summary="stub",
        evidence={},
    )
    defaults.update(overrides)
    return Signal(signal_id=signal_id, entity=entity, magnitude=magnitude, **defaults)


class TestRecommendationSet:
    def test_the_known_recommendations_are_produced(self, recommendations):
        assert set(_by_id(recommendations)) == {
            "reallocate_meta_to_google",
            "restock_cbd_muscle_balm",
            "restock_cbd_oil_20",
            "restock_vitamin_d3_drops",
            "investigate_vitamin_d3_drops_mix_economics",
            "investigate_email_tracking",
            "refresh_flow_win_back",
            "no_action_aov_decline",
            "no_action_cohort_retention_collapse",
        }

    def test_ids_are_safe_for_urls_and_logs(self, recommendations):
        """Product names contain characters like '%' ("CBD Oil 20%") that must
        not leak into identifiers."""
        for rec in recommendations:
            assert re.fullmatch(r"[a-z0-9_]+", rec.recommendation_id), rec.recommendation_id


class TestBudgetReallocation:
    def test_direction_and_sizing(self, recommendations):
        rec = _by_id(recommendations)["reallocate_meta_to_google"]
        params = rec.parameters
        assert params["source_channel"] == "Meta"
        assert params["destination_channel"] == "Google"
        assert 0 < params["monthly_shift_gbp"] <= params["source_monthly_spend"] * 0.30
        assert params["monthly_shift_gbp"] <= params["headroom_after_haircut"]

    def test_every_corroborating_check_holds_on_this_data(self, recommendations):
        rec = _by_id(recommendations)["reallocate_meta_to_google"]
        assert rec.confidence.corroboration == 1.0
        assert not any("check failed" in n.lower() for n in rec.confidence.notes)

    def test_needs_both_impairment_signals(self, con):
        assert rule_reallocate_paid_budget([], con) == []
        only_cpc = [_stub_signal("cpc_inflation_meta", "Meta", 1.4)]
        assert rule_reallocate_paid_budget(only_cpc, con) == []

    def test_does_not_recommend_moving_money_into_a_broken_channel(self, con):
        """The generalised rule with the impairment inverted: were Google the
        impaired channel, Meta would be the only candidate destination, and
        Meta is below break-even, so the rule must stay silent rather than
        recommend feeding the worse channel."""
        inverted = [
            _stub_signal("cpc_inflation_google", "Google", 1.4, persistence_days=60),
            _stub_signal("payback_breach_google", "Google", -0.5, persistence_days=20),
        ]
        assert rule_reallocate_paid_budget(inverted, con) == []


class TestRestock:
    def test_orders_are_consolidated_per_product(self, recommendations):
        restocks = [r for r in recommendations if r.recommendation_id.startswith("restock_")]
        assert len(restocks) == 3
        for rec in restocks:
            assert rec.parameters["total_reorder_units"] > 0
            assert rec.parameters["total_reorder_cost"] > 0

    def test_only_the_surged_product_carries_reversion_risk(self, recommendations):
        index = _by_id(recommendations)
        assert index["restock_vitamin_d3_drops"].parameters["demand_recently_surged"]
        assert not index["restock_cbd_oil_20"].parameters["demand_recently_surged"]
        assert index["restock_vitamin_d3_drops"].parameters["demand_reversion_ratio"] < 1.0

    def test_demand_check_fails_when_a_variant_is_in_decline(self, con, signals):
        """Falsifiability of demand_is_not_in_detected_decline: inject a
        detected decline for a CBD Oil SKU and the corresponding restock's
        corroboration must drop, with the failure named in the notes."""
        decline = _stub_signal(
            "velocity_vit_cbd20_30ml", "VIT-CBD20-30ML", -0.4,
            metric_family="product_velocity",
        )
        recommendations = rule_restock_critical_inventory(list(signals) + [decline], con)
        rec = _by_id(recommendations)["restock_cbd_oil_20"]
        assert rec.confidence.corroboration < 1.0
        assert any("demand_is_not_in_detected_decline" in n for n in rec.confidence.notes)


class TestMixInvestigation:
    def test_product_is_derived_from_the_signal_not_hardcoded(self, recommendations):
        rec = _by_id(recommendations)["investigate_vitamin_d3_drops_mix_economics"]
        assert rec.parameters["product"] == "Vitamin D3 Drops"
        assert rec.parameters["profit_per_unit"] < rec.parameters["catalogue_profit_per_unit"]


class TestEmailRules:
    def test_incident_simultaneity_is_measured_not_asserted(self, recommendations):
        rec = _by_id(recommendations)["investigate_email_tracking"]
        assert rec.parameters["onset_spread_days"] is not None
        assert rec.parameters["onset_spread_days"] <= 28
        assert rec.confidence.corroboration == 1.0

    def test_refresh_check_fails_for_a_flow_the_incident_explains(self, con, signals):
        """Falsifiability of not_explained_by_account_tracking_incident: hand
        the rule a commercially-declining flow that the account incident also
        claims, and the check must fail."""
        incident = next(s for s in signals if s.signal_id == "email_tracking_incident_account")
        claimed_flow = incident.evidence["flows_affected"][0]
        conflicting = _stub_signal(
            f"email_divergence_{claimed_flow.lower().replace(' ', '_')}",
            claimed_flow,
            -0.2,
            metric_family="email_engagement",
            evidence={"order_rate_change": -0.25, "open_rate_change": -0.2},
        )
        [rec] = rule_refresh_declining_flow([incident, conflicting], con)
        assert any(
            "not_explained_by_account_tracking_incident" in n for n in rec.confidence.notes
        )


class TestSuppressions:
    def test_the_artifact_is_suppressed_with_zero_confidence(self, recommendations):
        rec = _by_id(recommendations)["no_action_cohort_retention_collapse"]
        assert rec.confidence.overall == 0.0, "LTV data quality is zero, and it multiplies"

    def test_the_aov_suppression_rests_on_the_recorded_decomposition(self, recommendations):
        rec = _by_id(recommendations)["no_action_aov_decline"]
        assert rec.confidence.corroboration == 1.0
        assert rec.parameters.get("mix_driven") is True
