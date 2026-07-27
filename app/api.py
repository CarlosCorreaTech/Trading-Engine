"""HTTP surface for the demo console.

Small on purpose. The pipeline runs once at startup and every endpoint reads
from that one result, so the API is a view onto a computation rather than a
thing that computes. The only write-ish endpoint re-runs the pipeline, which
exists so the console can be refreshed after the warehouse is rebuilt without
restarting the container.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.service import get_bundle
from src.config import WAREHOUSE_PATH

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Built here rather than assumed, so a fresh checkout or a container with an
    # empty volume comes up working instead of returning 500s that look like a
    # bug in the app.
    if not WAREHOUSE_PATH.exists():
        from src.build import build

        build(verbose=False).close()
    get_bundle()
    yield


app = FastAPI(
    title="Trading Engine",
    description="Decision console for the trading engine.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/api/overview")
def overview():
    return get_bundle().overview


@app.get("/api/decisions")
def decisions():
    bundle = get_bundle()
    return {"groups": bundle.groups, "decisions": bundle.decisions}


@app.get("/api/decisions/{decision_id}")
def decision(decision_id: str):
    match = next((d for d in get_bundle().decisions if d["id"] == decision_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"No decision {decision_id!r}")
    return match


@app.get("/api/signals")
def signals():
    return get_bundle().signals


@app.get("/api/quality")
def quality():
    return get_bundle().quality


@app.post("/api/refresh")
def refresh():
    bundle = get_bundle(refresh=True)
    return {"decisions": len(bundle.decisions), "signals": len(bundle.signals)}


@app.get("/healthz")
def healthz():
    return {"status": "ok", "warehouse": WAREHOUSE_PATH.exists()}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/", StaticFiles(directory=STATIC), name="static")
