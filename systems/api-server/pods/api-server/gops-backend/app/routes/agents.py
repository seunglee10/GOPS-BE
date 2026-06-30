import asyncio
import time
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.contracts.agents import AgentAnalysisRequest
from app.core.config import read_dotenv_value
from app.services.agent_alert_payloads import parse_pubsub_payload
from app.services.agent_gateway import get_agent_report, request_agent_analysis

router = APIRouter()
AGENT_ALERTS_CHANNEL = "agent.alerts"


@router.post("/api/agents/analyze")
def analyze_agents(request: AgentAnalysisRequest) -> dict[str, Any]:
    return request_agent_analysis(request.model_dump())


@router.get("/api/agents/reports/{analysis_id}")
def agent_report(analysis_id: str) -> dict[str, Any]:
    return get_agent_report(analysis_id)


@router.websocket("/ws/agent-alerts")
async def agent_alerts(
    websocket: WebSocket,
    symbol: str | None = Query(default=None, min_length=1, max_length=12),
) -> None:
    await websocket.accept()
    try:
        await websocket.send_json({"type": "AGENT_ALERTS_READY", "symbol": symbol})
        await stream_agent_alerts(websocket, symbol.upper() if symbol else None)
    except WebSocketDisconnect:
        return


async def stream_agent_alerts(websocket: WebSocket, symbol: str | None) -> None:
    import redis

    redis_url = read_dotenv_value("REDIS_URL") or "redis://localhost:6379/0"
    client = redis.from_url(redis_url, decode_responses=True)
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    channels = [AGENT_ALERTS_CHANNEL]
    if symbol:
        channels.append(f"{AGENT_ALERTS_CHANNEL}:{symbol}")
    pubsub.subscribe(*channels)
    last_heartbeat = 0.0
    try:
        while True:
            message = await asyncio.to_thread(pubsub.get_message, timeout=1.0)
            if message and message.get("type") == "message":
                await websocket.send_json(parse_pubsub_payload(message.get("data")))
                continue
            now = time.monotonic()
            if now - last_heartbeat >= 25:
                last_heartbeat = now
                await websocket.send_json({"type": "HEARTBEAT", "source": "agent-alerts"})
    finally:
        pubsub.close()
