"""Shared fixtures for the integration tests.

The unit tests (statistics, confidence, simulation machinery, gate) run on
synthetic inputs and need none of this. The integration tests run the real
detectors and rules against the real warehouse, because most of what those
layers can get wrong -- a query that no longer matches the schema, a rule that
quietly stops firing, a check that can never fail -- only shows up against
actual data.

Everything is session-scoped: the warehouse is read-only for these tests and
the pipeline is deterministic, so computing signals and recommendations once
keeps the suite fast without hiding anything.
"""

from __future__ import annotations

import pytest

from src.config import WAREHOUSE_PATH


@pytest.fixture(scope="session")
def con():
    if not WAREHOUSE_PATH.exists():
        from src.build import build

        build(verbose=False).close()
    from src.warehouse import connect

    connection = connect()
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def quality(con):
    from src.warehouse import quality_scores

    return quality_scores(con)


@pytest.fixture(scope="session")
def signals(con):
    from src.detection.runner import detect_all

    return detect_all(con)


@pytest.fixture(scope="session")
def recommendations(con, signals):
    from src.recommendations.engine import recommend

    return recommend(signals, con)
