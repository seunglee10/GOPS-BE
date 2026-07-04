from typing import Any

from fastapi import HTTPException

from app.contracts.chart import AgentChatRequest
from app.core.config import read_dotenv_value
from app.services.alfaka_market_data import configured_symbols
from gops_agents.chart_command import (
    ANALYSIS_KEYWORDS,
    ANALYSIS_TIMEFRAMES,
    ChartCommandAgent,
    ChartCommandError,
    chart_context_for_agent_prompt,
    extract_openai_error_detail,
    extract_response_text,
    is_chart_analysis_request,
    is_live_feed_status_request,
)
from gops_agents.chart_command import (
    build_agent_market_analysis_context as _build_agent_market_analysis_context,
)
from gops_agents.chart_command import (
    request_openai_response as _request_openai_response,
)


def _agent() -> ChartCommandAgent:
    return ChartCommandAgent(
        read_config=read_dotenv_value,
        configured_symbols=configured_symbols,
        response_requester=request_openai_response,
    )


def _map_chart_command_error(error: ChartCommandError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail)


def openai_agent_chat(request: AgentChatRequest) -> dict[str, Any]:
    try:
        return _agent().chat(request)
    except ChartCommandError as exc:
        raise _map_chart_command_error(exc) from exc


def openai_chart_proposal(context: dict[str, Any]) -> dict[str, Any]:
    try:
        return _agent().chart_proposal(context)
    except ChartCommandError as exc:
        raise _map_chart_command_error(exc) from exc


def request_openai_response(payload: dict[str, Any]) -> str:
    try:
        return _request_openai_response(payload, read_config=read_dotenv_value)
    except ChartCommandError as exc:
        raise _map_chart_command_error(exc) from exc


def build_agent_market_analysis_context(context: dict[str, Any]) -> dict[str, Any]:
    return _build_agent_market_analysis_context(context, configured_symbols=configured_symbols)
