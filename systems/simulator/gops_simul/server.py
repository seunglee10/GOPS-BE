from __future__ import annotations

import argparse
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from gops_simul.auth import validate_credentials, validate_rest_auth
from gops_simul.config import Settings
from gops_simul.dashboard import render_control_dashboard
from gops_simul.demo import DemoScenarioController, load_demo_scenario
from gops_simul.errors import SimulatorError, Unauthorized
from gops_simul.replay import WebSocketState
from gops_simul.storage import SessionStore, parse_symbols_csv


class HealthResponse(BaseModel):
    status: str
    dataRoot: str
    authMode: str


class ModeRequest(BaseModel):
    mode: str


class ActionRequest(BaseModel):
    action: str


class PhaseRequest(BaseModel):
    phase: str


class BasketOrderRequest(BaseModel):
    userId: str = "demo-user"
    basket: str
    side: str


class IndividualOrderRequest(BaseModel):
    userId: str = "demo-user"
    symbol: str
    side: str
    quantity: int


def create_app(
    settings: Settings | None = None,
    *,
    demo_controller: DemoScenarioController | None = None,
) -> FastAPI:
    load_default_demo = settings is None and demo_controller is None
    settings = settings or Settings.from_env()
    store = SessionStore(settings)
    if load_default_demo:
        demo_controller = DemoScenarioController(load_demo_scenario())
    app = FastAPI(title="GOPS Alpaca-Compatible Market Data Simulator", version="0.1.0")
    app.state.settings = settings
    app.state.store = store
    app.state.demo_controller = demo_controller

    def require_demo_controller() -> DemoScenarioController:
        if demo_controller is None:
            raise HTTPException(status_code=503, detail="demo scenario is not configured")
        return demo_controller

    @app.exception_handler(SimulatorError)
    async def simulator_error_handler(_request: Request, exc: SimulatorError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"message": str(exc), "code": exc.code},
        )

    @app.get("/health", response_model=HealthResponse)
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "dataRoot": str(settings.data_root),
            "authMode": settings.auth_mode,
        }

    @app.get("/", response_class=HTMLResponse)
    def demo_dashboard() -> str:
        return render_control_dashboard()

    @app.get("/api/control/status")
    def demo_status() -> dict[str, object]:
        return require_demo_controller().status()

    @app.put("/api/control/mode")
    def set_demo_mode(payload: ModeRequest) -> dict[str, object]:
        try:
            return require_demo_controller().set_mode(payload.mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/control/action")
    def demo_action(payload: ActionRequest) -> dict[str, object]:
        try:
            controller = require_demo_controller()
            if payload.action == "pause":
                return controller.pause()
            if payload.action == "resume":
                return controller.resume()
            if payload.action == "restart":
                return controller.restart()
            raise ValueError("action must be pause, resume, or restart")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/control/phase")
    def set_demo_phase(payload: PhaseRequest) -> dict[str, object]:
        try:
            return require_demo_controller().set_phase(payload.phase)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/control/account")
    def demo_account(userId: str = Query(default="demo-user")) -> dict[str, object]:
        controller = require_demo_controller()
        if controller.status()["mode"] != "simulation":
            raise HTTPException(status_code=409, detail="simulation mode is not active")
        return controller.account(userId)

    @app.post("/api/control/orders/basket")
    def demo_basket_order(payload: BasketOrderRequest) -> dict[str, object]:
        try:
            return require_demo_controller().submit_basket_order(
                payload.userId,
                basket=payload.basket,
                side=payload.side,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/control/orders")
    def demo_individual_order(payload: IndividualOrderRequest) -> dict[str, object]:
        try:
            return require_demo_controller().submit_order(
                payload.userId,
                symbol=payload.symbol,
                side=payload.side,
                quantity=payload.quantity,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1beta1/news")
    def demo_news(
        symbols: str | None = Query(default=None),
        limit: int = Query(default=10, ge=1, le=50),
    ) -> dict[str, object]:
        del limit
        requested = parse_symbols_csv(symbols) if symbols else []
        return require_demo_controller().news(requested)

    @app.get("/v2/stocks/bars")
    def stock_bars(
        request: Request,
        symbols: str = Query(min_length=1),
        timeframe: str = Query(default="1Min"),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
        limit: int = Query(default=1000, ge=1, le=10_000),
        adjustment: str = Query(default="raw"),
        feed: str = Query(default="sip"),
        page_token: str | None = Query(default=None),
        sort: str = Query(default="asc", pattern="^(asc|desc)$"),
    ) -> dict[str, object]:
        validate_rest_auth(request, settings)
        return store.query(
            kind="bars",
            symbols=parse_symbols_csv(symbols),
            feed=feed,
            start=start,
            end=end,
            limit=limit,
            sort=sort,
            page_token=page_token,
            timeframe=timeframe,
            adjustment=adjustment,
        ).payload

    @app.get("/v2/stocks/trades")
    def stock_trades(
        request: Request,
        symbols: str = Query(min_length=1),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
        limit: int = Query(default=1000, ge=1, le=10_000),
        feed: str = Query(default="sip"),
        page_token: str | None = Query(default=None),
        sort: str = Query(default="asc", pattern="^(asc|desc)$"),
    ) -> dict[str, object]:
        validate_rest_auth(request, settings)
        return store.query(
            kind="trades",
            symbols=parse_symbols_csv(symbols),
            feed=feed,
            start=start,
            end=end,
            limit=limit,
            sort=sort,
            page_token=page_token,
        ).payload

    @app.get("/v2/stocks/quotes")
    def stock_quotes(
        request: Request,
        symbols: str = Query(min_length=1),
        start: str | None = Query(default=None),
        end: str | None = Query(default=None),
        limit: int = Query(default=1000, ge=1, le=10_000),
        feed: str = Query(default="sip"),
        page_token: str | None = Query(default=None),
        sort: str = Query(default="asc", pattern="^(asc|desc)$"),
    ) -> dict[str, object]:
        validate_rest_auth(request, settings)
        return store.query(
            kind="quotes",
            symbols=parse_symbols_csv(symbols),
            feed=feed,
            start=start,
            end=end,
            limit=limit,
            sort=sort,
            page_token=page_token,
        ).payload

    @app.get("/v2/assets")
    def assets(
        request: Request,
        status: str | None = Query(default="active"),
        asset_class: str | None = Query(default="us_equity"),
    ) -> list[dict[str, object]]:
        validate_rest_auth(request, settings)
        return store.assets(status=status, asset_class=asset_class)

    async def handle_market_data_socket(websocket: WebSocket, feed: str) -> None:
        store.validate_feed(feed)
        state = WebSocketState(
            feed=feed,
            settings=settings,
            store=store,
            demo_controller=demo_controller,
        )
        await websocket.accept()
        await state.send_json(websocket, [{"T": "success", "msg": "connected"}])
        try:
            try:
                validate_credentials(
                    websocket.headers.get("APCA-API-KEY-ID"),
                    websocket.headers.get("APCA-API-SECRET-KEY"),
                    settings,
                )
                state.authenticated = True
                await state.send_json(websocket, [{"T": "success", "msg": "authenticated"}])
            except Unauthorized:
                pass

            while True:
                raw = await websocket.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await state.send_json(websocket, [{"T": "error", "code": 400, "msg": "invalid syntax"}])
                    continue
                if not isinstance(message, dict):
                    await state.send_json(websocket, [{"T": "error", "code": 400, "msg": "invalid syntax"}])
                    continue
                action = str(message.get("action") or "").strip().lower()
                if action == "auth":
                    try:
                        validate_credentials(str(message.get("key") or ""), str(message.get("secret") or ""), settings)
                    except Unauthorized:
                        await state.send_json(websocket, [{"T": "error", "code": 402, "msg": "auth failed"}])
                        continue
                    if state.authenticated:
                        await state.send_json(websocket, [{"T": "error", "code": 403, "msg": "already authenticated"}])
                    else:
                        state.authenticated = True
                        await state.send_json(websocket, [{"T": "success", "msg": "authenticated"}])
                    continue
                if not state.authenticated:
                    await state.send_json(websocket, [{"T": "error", "code": 401, "msg": "not authenticated"}])
                    continue
                if action == "subscribe":
                    try:
                        await state.subscribe(websocket, message)
                    except ValueError as exc:
                        await state.send_json(websocket, [{"T": "error", "code": 400, "msg": str(exc)}])
                    continue
                if action == "unsubscribe":
                    try:
                        await state.unsubscribe(websocket, message)
                    except ValueError as exc:
                        await state.send_json(websocket, [{"T": "error", "code": 400, "msg": str(exc)}])
                    continue
                await state.send_json(websocket, [{"T": "error", "code": 400, "msg": "invalid syntax"}])
        except WebSocketDisconnect:
            return
        finally:
            state.cancel_replay()

    @app.websocket("/v2/{feed}")
    async def v2_stream(websocket: WebSocket, feed: str) -> None:
        try:
            await handle_market_data_socket(websocket, feed)
        except SimulatorError as exc:
            await websocket.accept()
            await websocket.send_text(json.dumps([{"T": "error", "code": exc.status_code, "msg": str(exc)}]))
            await websocket.close()

    @app.websocket("/v1beta1/{feed}")
    async def v1beta1_stream(websocket: WebSocket, feed: str) -> None:
        try:
            await handle_market_data_socket(websocket, feed)
        except SimulatorError as exc:
            await websocket.accept()
            await websocket.send_text(json.dumps([{"T": "error", "code": exc.status_code, "msg": str(exc)}]))
            await websocket.close()

    return app


app = create_app()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the GOPS Alpaca-compatible market-data simulator.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run("gops_simul.server:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
