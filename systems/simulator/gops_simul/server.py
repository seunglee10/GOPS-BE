from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import suppress
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from gops_simul.clickhouse import ClickHouseHttpClient, ClickHouseReplayEventSource
from gops_simul.config import Settings
from gops_simul.dataset import ALLOWED_SPEEDS, DATASET_ID, REPLAY_SYMBOLS
from gops_simul.tick_replay import InMemoryReplayEventSource, ReplayController
from gops_simul.state_store import RedisReplayStateStore


class ModeRequest(BaseModel):
    mode: Literal["live", "simulation"]


class ActionRequest(BaseModel):
    action: Literal["start", "pause", "resume", "restart"]


class SpeedRequest(BaseModel):
    speed: Literal[1, 5, 20, 60]


def build_default_controller(settings: Settings) -> ReplayController:
    redis_url = os.getenv("REDIS_URL", "").strip()
    state_store = RedisReplayStateStore.from_url(redis_url) if redis_url else None
    clickhouse_url = os.getenv("CLICKHOUSE_URL", "").strip()
    if not clickhouse_url:
        return ReplayController(
            InMemoryReplayEventSource([]),
            default_speed=int(settings.replay_speed),
            state_store=state_store,
        )
    client = ClickHouseHttpClient(
        clickhouse_url,
        database=os.getenv("CLICKHOUSE_DATABASE", "market_data"),
        user=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        timeout_seconds=float(os.getenv("SIM_CLICKHOUSE_TIMEOUT_SECONDS", "30")),
    )
    return ReplayController(
        ClickHouseReplayEventSource(client, os.getenv("SIM_REPLAY_DATASET_ID", DATASET_ID)),
        default_speed=int(settings.replay_speed),
        max_events_per_pump=int(os.getenv("SIM_MAX_EVENTS_PER_PUMP", "50000")),
        state_store=state_store,
    )


def create_app(
    settings: Settings | None = None,
    *,
    replay_controller: ReplayController | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    controller = replay_controller or build_default_controller(settings)
    app = FastAPI(title="GOPS Tick Replay Simulator", version="1.0.0")
    app.state.settings = settings
    app.state.replay_controller = controller
    pump_task: asyncio.Task[None] | None = None

    async def pump_replay() -> None:
        while True:
            await asyncio.to_thread(controller.status)
            await asyncio.sleep(0.01 if controller.state == "running" else 0.25)

    @app.on_event("startup")
    async def start_pump() -> None:
        nonlocal pump_task
        pump_task = asyncio.create_task(pump_replay())

    @app.on_event("shutdown")
    async def stop_pump() -> None:
        if pump_task is None:
            return
        pump_task.cancel()
        with suppress(asyncio.CancelledError):
            await pump_task

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "datasetId": controller.source.dataset_id,
            "datasetReady": controller.source.total_events > 0,
            "totalEventCount": controller.source.total_events,
        }

    @app.get("/api/control/status")
    def replay_status() -> dict[str, object]:
        return {"available": controller.source.total_events > 0, **controller.status_snapshot()}

    @app.put("/api/control/mode")
    def set_mode(payload: ModeRequest) -> dict[str, object]:
        if payload.mode == "simulation" and controller.source.total_events <= 0:
            raise HTTPException(status_code=503, detail=f"replay dataset {controller.source.dataset_id} is not READY")
        try:
            return {"available": True, **controller.set_mode(payload.mode)}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/control/action")
    def replay_action(payload: ActionRequest) -> dict[str, object]:
        try:
            if payload.action == "start":
                result = controller.start()
            elif payload.action == "pause":
                result = controller.pause()
            elif payload.action == "resume":
                result = controller.resume()
            else:
                result = controller.restart()
            return {"available": True, **result}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/api/control/speed")
    def replay_speed(payload: SpeedRequest) -> dict[str, object]:
        try:
            return {"available": True, **controller.set_speed(payload.speed)}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/control/quote")
    def replay_quote(symbol: str = Query(min_length=1, max_length=12)) -> dict[str, object]:
        quote = controller.latest_quote_details(symbol)
        if quote is None:
            raise HTTPException(status_code=404, detail="current replay quote is unavailable")
        return {"symbol": symbol.strip().upper(), **quote, "runId": controller.run_id}

    @app.get("/api/control/execution-events")
    def replay_execution_events(
        runId: str = Query(min_length=1, max_length=100),
        afterSequence: int = Query(default=0, ge=0),
        limit: int = Query(default=50_000, ge=1, le=50_000),
    ) -> dict[str, object]:
        if controller.mode != "simulation" or controller.run_id != runId:
            raise HTTPException(status_code=409, detail="simulation run is not active")
        try:
            return controller.execution_events(after_sequence=afterSequence, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/control/candles")
    def replay_candles(
        symbol: str = Query(min_length=1, max_length=12),
        interval: str = Query(default="1m"),
        limit: int = Query(default=2000, ge=1, le=5000),
    ) -> dict[str, object]:
        if controller.mode != "simulation":
            raise HTTPException(status_code=409, detail="simulation mode is not active")
        try:
            return controller.candle_snapshot(symbol, interval, limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/control/symbols")
    def replay_symbols(q: str = "", limit: int = Query(default=100, ge=1, le=100)) -> dict[str, object]:
        query = q.strip().upper()
        symbols = [symbol for symbol in REPLAY_SYMBOLS if not query or query in symbol][:limit]
        return {
            "source": "simulation_replay",
            "datasetId": controller.source.dataset_id,
            "symbols": [{"symbol": symbol, "name": symbol, "market": "US", "tradable": True} for symbol in symbols],
        }

    return app


app = create_app()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the GOPS tick replay simulator.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run("gops_simul.server:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
