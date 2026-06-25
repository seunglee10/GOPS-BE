from __future__ import annotations

import asyncio

from fastapi import FastAPI, Query, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.dummy_market import DEFAULT_SYMBOLS, DummyMarketData
from backend.llm_client import LlmResponseInvalid, request_chat_completion
from backend.market_stream import market_websocket
from backend.schemas import ChatErrorResponse, ChatRequest
from backend.settings import get_settings


app = FastAPI(title="GOPS Chart MVP API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
market = DummyMarketData()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/market/snapshot")
def market_snapshot(
    symbols: str = Query(default="AAPL"),
    timeframe: str = Query(default="1m"),
    limit: int = Query(default=300, ge=1, le=1000),
):
    try:
        symbol_list = market.validate_symbols(symbols.split(",") if symbols else DEFAULT_SYMBOLS)
        timeframe_value = market.validate_timeframe(timeframe)
    except ValueError as error:
        return JSONResponse(status_code=400, content={"error": {"code": "invalid_market_request", "message": str(error)}})
    return market.snapshot(symbol_list, timeframe_value, limit).model_dump()


@app.websocket("/ws/market")
async def ws_market(websocket: WebSocket) -> None:
    await market_websocket(websocket, market)


@app.post("/api/chat")
async def chat(chat_request: ChatRequest):
    settings = get_settings()
    if not settings.openai_api_key:
        return _chat_error(503, "openai_api_key_missing", "OPENAI_API_KEY is not configured on the backend.")
    try:
        response = await request_chat_completion(chat_request, settings)
        return response.model_dump()
    except TimeoutError:
        return _chat_error(504, "openai_timeout", "The OpenAI request timed out.")
    except asyncio.TimeoutError:
        return _chat_error(504, "openai_timeout", "The OpenAI request timed out.")
    except LlmResponseInvalid as error:
        return _chat_error(502, "llm_response_invalid", str(error))
    except Exception:
        return _chat_error(502, "openai_request_failed", "The backend could not complete the OpenAI request.")


def _chat_error(status_code: int, code: str, message: str) -> JSONResponse:
    body = ChatErrorResponse(error={"code": code, "message": message})  # type: ignore[arg-type]
    return JSONResponse(status_code=status_code, content=body.model_dump())
