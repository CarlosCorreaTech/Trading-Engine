"""Integration tests: the console API end to end.

The client is opened as a context manager so the lifespan hook runs, which is
the same startup path the container takes: build the warehouse if it is
missing, run the pipeline once, serve everything from the cached result.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


class TestEndpoints:
    def test_healthz(self, client):
        body = client.get("/healthz").json()
        assert body["status"] == "ok"
        assert body["warehouse"] is True

    def test_overview_carries_the_headline_numbers(self, client):
        overview = client.get("/api/overview").json()
        assert overview["signals"] >= 1
        assert overview["decisions"] >= 1
        assert overview["monthly_gross_profit"] > 0

    def test_decisions_are_grouped_by_verdict(self, client):
        body = client.get("/api/decisions").json()
        assert body["decisions"], "the pipeline should produce decisions"
        group_verdicts = [g["verdict"] for g in body["groups"]]
        assert group_verdicts == sorted(
            group_verdicts,
            key=["auto_execute", "propose", "escalate", "suppress"].index,
        )
        total_in_groups = sum(len(g["ids"]) for g in body["groups"])
        assert total_in_groups == len(body["decisions"])

    def test_single_decision_lookup_and_miss(self, client):
        decisions = client.get("/api/decisions").json()["decisions"]
        some_id = decisions[0]["id"]
        assert client.get(f"/api/decisions/{some_id}").json()["id"] == some_id
        assert client.get("/api/decisions/not_a_decision").status_code == 404

    def test_simulated_decisions_ship_their_distributions(self, client):
        decisions = client.get("/api/decisions").json()["decisions"]
        simulated = [d for d in decisions if "distribution" in (d.get("simulation") or {})]
        assert simulated, "cash actions should carry binned simulation output"
        for decision in simulated:
            nominal = decision["simulation"]["distribution"]["nominal"]
            assert sum(nominal["counts"]) > 0
            assert nominal["p5"] <= nominal["p50"] <= nominal["p95"]

    def test_signals_and_quality_endpoints(self, client):
        signals = client.get("/api/signals").json()
        assert {s["classification"] for s in signals} >= {"commercial", "artifact"}
        quality = client.get("/api/quality").json()
        assert quality["checks"], "data quality check results should be listed"
        assert quality["scores"], "metric families with scores should be listed"

    def test_the_console_itself_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "<!doctype html>" in response.text.lower()
