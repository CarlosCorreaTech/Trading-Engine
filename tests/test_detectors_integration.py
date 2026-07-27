"""Integration tests: the detectors against the real warehouse.

These pin what the detection layer actually finds in this dataset. They are
deliberately specific -- named signals, directions, classifications -- because
the failure mode they guard against is not a wrong number but a silent one: a
detector whose query drifts out of step with the schema returns an empty list,
and every downstream layer handles an empty list gracefully.
"""

from __future__ import annotations

import datetime as dt

from src.config import ANALYSIS_END, DETECTION
from src.detection.signal import Classification


def _by_id(signals):
    return {s.signal_id: s for s in signals}


class TestSignalSet:
    def test_signal_ids_are_unique(self, signals):
        ids = [s.signal_id for s in signals]
        assert len(ids) == len(set(ids))

    def test_the_known_signal_set_is_found(self, signals):
        """Pinned to the exact set so a detector going quiet fails loudly."""
        assert set(_by_id(signals)) == {
            "cpc_inflation_meta",
            "payback_breach_meta",
            "velocity_vit_d3_30ml",
            "velocity_vit_d3_30ml_4k",
            "stockout_risk_vit_d3_30ml",
            "stockout_risk_vit_d3_30ml_4k",
            "stockout_risk_vit_cbd20_30ml",
            "stockout_risk_vit_balm_50ml",
            "stockout_risk_vit_balm_100ml",
            "aov_decline",
            "cohort_retention_collapse",
            "email_tracking_incident_account",
            "email_divergence_browse_abandonment",
            "email_divergence_cart_abandonment",
            "email_divergence_post_purchase",
            "email_divergence_welcome_series",
            "email_divergence_win_back",
        }


class TestPaidChannelDetectors:
    def test_meta_cpc_inflation_is_found_and_google_is_clean(self, signals):
        index = _by_id(signals)
        assert "cpc_inflation_google" not in index
        assert "payback_breach_google" not in index
        cpc = index["cpc_inflation_meta"]
        assert cpc.classification is Classification.COMMERCIAL
        assert cpc.direction == "increase"
        assert cpc.magnitude > 1.0, "the Meta CPC rise is over 100%, not marginal"

    def test_cpc_baseline_predates_the_change(self, signals):
        """The origin fix: the baseline must stop where the change began, so
        the reported baseline has to sit well below the current level rather
        than partway up the ramp."""
        cpc = _by_id(signals)["cpc_inflation_meta"]
        changepoint = dt.date.fromisoformat(cpc.evidence["changepoint_date"])
        assert changepoint <= cpc.detected_at
        assert cpc.baseline_value < cpc.current_value / 2

    def test_meta_payback_is_below_break_even(self, signals):
        payback = _by_id(signals)["payback_breach_meta"]
        assert payback.current_value < 1.0
        assert payback.baseline_value > 1.0, "the breach is a change, not a permanent state"


class TestVelocityAndInventory:
    def test_d3_surge_is_detected_on_both_variants(self, signals):
        index = _by_id(signals)
        for signal_id in ("velocity_vit_d3_30ml", "velocity_vit_d3_30ml_4k"):
            signal = index[signal_id]
            assert signal.direction == "increase"
            assert signal.magnitude > 1.0
            # Share-based detection dates the surge to winter, not to the
            # Christmas volume peak in the raw units.
            assert dt.date(2024, 12, 1) <= signal.detected_at <= dt.date(2025, 3, 1)

    def test_stockout_signals_sit_below_the_threshold_as_of_the_data_end(self, signals):
        risks = [s for s in signals if s.signal_id.startswith("stockout_risk_")]
        assert risks
        for signal in risks:
            assert signal.current_value < DETECTION["inventory_cover_days_critical"]
            assert signal.detected_at == dt.date.fromisoformat(ANALYSIS_END)


class TestEmailIncident:
    def test_simultaneous_divergences_consolidate_into_one_incident(self, signals):
        incidents = [s for s in signals if s.signal_id == "email_tracking_incident_account"]
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident.classification is Classification.DATA_QUALITY
        assert incident.evidence["flow_count"] >= 2

    def test_affected_flows_are_demoted_so_they_cannot_spawn_recommendations(self, signals):
        index = _by_id(signals)
        incident = index["email_tracking_incident_account"]
        for flow in incident.evidence["flows_affected"]:
            flow_signal = next(s for s in signals if s.entity == flow)
            assert flow_signal.classification is Classification.EXPLAINED

    def test_win_back_decline_stays_commercial(self, signals):
        """Win-Back's orders fell with its engagement, so it must not be
        absorbed into the tracking incident."""
        index = _by_id(signals)
        win_back = index["email_divergence_win_back"]
        assert win_back.classification is Classification.COMMERCIAL
        assert win_back.entity not in set(
            index["email_tracking_incident_account"].evidence["flows_affected"]
        )

    def test_incident_records_per_flow_onsets(self, signals):
        incident = _by_id(signals)["email_tracking_incident_account"]
        onsets = incident.evidence["flow_onsets"]
        assert set(onsets) == set(incident.evidence["flows_affected"])
        parsed = sorted(dt.date.fromisoformat(d) for d in onsets.values())
        assert (parsed[-1] - parsed[0]).days <= 28, "onsets should be near-simultaneous"


class TestCohortAdjudication:
    def test_the_retention_collapse_is_ruled_an_artifact(self, signals):
        signal = _by_id(signals)["cohort_retention_collapse"]
        assert signal.classification is Classification.ARTIFACT
        assert signal.evidence["cohorts_with_zero_repeat"] >= 1

    def test_the_verdict_rests_on_more_than_one_test(self, signals):
        evidence = _by_id(signals)["cohort_retention_collapse"].evidence
        tests = [v for k, v in evidence.items() if k.startswith("test_")]
        assert sum(bool(t) for t in tests) >= 2

    def test_detected_at_is_the_first_dead_cohort_not_a_constant(self, signals):
        signal = _by_id(signals)["cohort_retention_collapse"]
        assert signal.detected_at == dt.date(2024, 12, 1)
        assert signal.detected_at.day == 1, "cohorts are monthly, so the date is a month start"


class TestAovDecline:
    def test_the_fall_is_explained_as_mix_driven(self, signals):
        signal = _by_id(signals)["aov_decline"]
        assert signal.classification is Classification.EXPLAINED
        assert signal.evidence["mix_driven"] is True
