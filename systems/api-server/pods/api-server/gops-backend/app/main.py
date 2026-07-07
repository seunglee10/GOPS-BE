import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.alerts.routes import router as alerts_router
from app.contracts.chart import AgentChatMessage, AgentChatRequest, ChartProposalRequest
from app.core.config import CORS_ORIGINS, read_dotenv_value
from app.market_data.indices.service import start_market_indices_warmer
from app.market_data.monitor.routes import router as market_monitor_router
from app.market_data.query.routes import router as market_query_router
from app.recommendations.routes import router as recommendations_router
from app.routes.account import account_holdings, router as account_router
from app.routes.auth import router as auth_router
from app.routes.agents import agent_alerts, agent_report, agent_report_stream, analyze_agents, router as agents_router
from app.routes.charts import chart_candles, chart_symbols, router as charts_router
from app.routes.health import health, log_runtime_config, router as health_router
from app.routes.llm import agent_chat, chart_proposal, router as llm_router
from app.routes.orders import order_contract, router as orders_router
from app.routes.streams import chart_stream, router as streams_router
from app.services.ai_agents import openai_agent_chat, openai_chart_proposal
from app.services.alfaka_market_data import configured_symbols, get_market_data_provider, symbol_summaries
from gops_agents.query_understanding import warm_entity_catalog_cache


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    warm_entity_catalog_cache()
    indices_warmer_task = start_market_indices_warmer()
    try:
        yield
    finally:
        if indices_warmer_task is not None:
            indices_warmer_task.cancel()
            with suppress(asyncio.CancelledError):
                await indices_warmer_task


def create_app() -> FastAPI:
    app = FastAPI(title="GOPS Backend Scaffold", version="0.1.0", lifespan=app_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(account_router)
    app.include_router(charts_router)
    app.include_router(market_query_router)
    app.include_router(market_monitor_router)
    app.include_router(agents_router)
    app.include_router(llm_router)
    app.include_router(orders_router)
    app.include_router(alerts_router)
    app.include_router(recommendations_router)
    app.include_router(streams_router)

    log_runtime_config()
    return app


app = create_app()

__all__ = [
    "AgentChatMessage",
    "AgentChatRequest",
    "ChartProposalRequest",
    "agent_alerts",
    "agent_chat",
    "agent_report",
    "agent_report_stream",
    "account_holdings",
    "analyze_agents",
    "app",
    "chart_candles",
    "chart_proposal",
    "chart_stream",
    "chart_symbols",
    "configured_symbols",
    "create_app",
    "get_market_data_provider",
    "health",
    "openai_agent_chat",
    "openai_chart_proposal",
    "order_contract",
    "read_dotenv_value",
    "symbol_summaries",
]
