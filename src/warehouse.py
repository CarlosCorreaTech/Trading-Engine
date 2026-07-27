"""Read-only access to the built warehouse.

Detection, recommendation and simulation all read through here so they share
one connection convention and one source for data-quality scores.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from src.config import WAREHOUSE_PATH


class WarehouseNotBuilt(RuntimeError):
    """Raised with instructions rather than a bare file-not-found."""


def connect(warehouse_path: Path = WAREHOUSE_PATH) -> duckdb.DuckDBPyConnection:
    if not warehouse_path.exists():
        raise WarehouseNotBuilt(
            f"{warehouse_path.name} not found. Build it first with: python -m src.build"
        )
    return duckdb.connect(str(warehouse_path), read_only=True)


def query(
    con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None
) -> pd.DataFrame:
    """Run a query, with values bound as parameters rather than formatted in.

    Every value here comes from config or from the warehouse itself, so this is
    not an injection defence; it is so that a value is never re-parsed as SQL,
    which is the difference between a threshold of 45.0 and one of 45.0e6 being
    a data mistake rather than a syntax accident.
    """
    return con.execute(sql, params).fetchdf() if params else con.execute(sql).fetchdf()


def quality_scores(con: duckdb.DuckDBPyConnection) -> dict[str, float]:
    """Trust score per metric family, keyed for direct lookup by detectors.

    Defaults to 1.0 for any family with no checks, which is the honest reading:
    absence of assertions is not evidence of a problem, though it is a reason to
    write some.
    """
    rows = con.execute("SELECT metric_family, quality_score FROM dq.metric_quality").fetchall()
    return {family: float(score) for family, score in rows}
