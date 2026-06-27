from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.contracts.chart import AgentChatMessage, AgentChatRequest, ChartProposalRequest
from backend.app.core.config import CORS_ORIGINS, read_dotenv_value
from backend.app.routes.charts import chart_candles, chart_symbols, router as charts_router
from backend.app.routes.health import health, router as health_router
from backend.app.routes.llm import agent_chat, chart_proposal, router as llm_router
from backend.app.routes.streams import chart_stream, router as streams_router
from backend.app.services.ai_agents import openai_agent_chat, openai_chart_proposal
from backend.app.services.market_data import build_dummy_candles, build_live_candle, build_symbol_summary, supported_dummy_symbols


def create_app() -> FastAPI:
    app = FastAPI(title="GOPS Backend Scaffold", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health_router)
    app.include_router(charts_router)
    app.include_router(llm_router)
    app.include_router(streams_router)
    return app


app = create_app()

__all__ = [
    "AgentChatMessage",
    "AgentChatRequest",
    "ChartProposalRequest",
    "agent_chat",
    "app",
    "build_dummy_candles",
    "build_live_candle",
    "build_symbol_summary",
    "chart_candles",
    "chart_proposal",
    "chart_stream",
    "chart_symbols",
    "create_app",
    "health",
    "openai_agent_chat",
    "openai_chart_proposal",
    "read_dotenv_value",
    "supported_dummy_symbols",
]
