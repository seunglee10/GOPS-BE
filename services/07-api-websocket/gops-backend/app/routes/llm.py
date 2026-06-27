from fastapi import APIRouter

from app.contracts.chart import AgentChatRequest, ChartProposalRequest
from app.core.config import read_dotenv_value
from app.services.ai_agents import fallback_agent_chat, fallback_chart_proposal, openai_agent_chat, openai_chart_proposal

router = APIRouter()


@router.post("/api/llm/chart-proposal")
def chart_proposal(request: ChartProposalRequest) -> dict[str, object]:
    if read_dotenv_value("GOPS_USE_MOCK_LLM") == "1":
        return {"proposal": fallback_chart_proposal(request.context)}
    return {"proposal": openai_chart_proposal(request.context)}


@router.post("/api/llm/chat")
def agent_chat(request: AgentChatRequest) -> dict[str, object]:
    if read_dotenv_value("GOPS_USE_MOCK_LLM") == "1":
        return fallback_agent_chat(request)
    return openai_agent_chat(request)
