"""Warehouse build runner.

Executes the SQL layers in dependency order into a single DuckDB file, so the
entire warehouse rebuilds from the raw CSVs with one command:

    python -m src.build

Files within a layer run in filename order, which is why they carry numeric
prefixes. The build is idempotent: it drops and recreates every object, so a
rerun always reflects the current SQL.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import duckdb

from src.config import DATA_DIR, SQL_LAYERS, SQL_DIR, WAREHOUSE_PATH


def _sql_files(layer: str) -> list[Path]:
    layer_dir = SQL_DIR / layer
    if not layer_dir.is_dir():
        return []
    return sorted(layer_dir.glob("*.sql"))


def connect(warehouse_path: Path = WAREHOUSE_PATH) -> duckdb.DuckDBPyConnection:
    """Open the warehouse with the data directory bound as a SQL variable.

    Binding the path as a variable rather than templating it into the SQL keeps
    every .sql file runnable as-is in the DuckDB CLI, which matters for anyone
    reviewing the models without running the Python.
    """
    con = duckdb.connect(str(warehouse_path))
    con.execute(f"SET VARIABLE data_dir = '{DATA_DIR.as_posix()}'")
    return con


def build(warehouse_path: Path = WAREHOUSE_PATH, verbose: bool = True) -> duckdb.DuckDBPyConnection:
    if warehouse_path.exists():
        warehouse_path.unlink()

    con = connect(warehouse_path)
    for layer in SQL_LAYERS:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {layer}")

    for layer in SQL_LAYERS:
        files = _sql_files(layer)
        if not files:
            continue
        if verbose:
            print(f"\n[{layer}]")
        for path in files:
            started = time.perf_counter()
            try:
                con.execute(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"Failed executing {path.relative_to(SQL_DIR.parent)}: {exc}") from exc
            if verbose:
                elapsed = (time.perf_counter() - started) * 1000
                print(f"  {path.stem:<40} {elapsed:7.0f} ms")

    if verbose:
        _print_summary(con)
    return con


def _print_summary(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema IN ('staging', 'core', 'semantic', 'dq')
        ORDER BY table_schema, table_name
        """
    ).fetchall()
    print("\n[objects]")
    for schema, table in rows:
        n = con.execute(f'SELECT count(*) FROM "{schema}"."{table}"').fetchone()[0]
        print(f"  {schema}.{table:<42} {n:>9,} rows")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the trading engine warehouse.")
    parser.add_argument("--quiet", action="store_true", help="suppress per-model output")
    args = parser.parse_args()

    started = time.perf_counter()
    con = build(verbose=not args.quiet)
    con.close()
    print(f"\nBuilt {WAREHOUSE_PATH.name} in {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
