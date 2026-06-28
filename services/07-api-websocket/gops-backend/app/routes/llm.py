from fastapi import APIRouter

from app.contracts.chart import AgentChatRequest, ChartProposalRequest
from app.services.ai_agents import openai_agent_chat, openai_chart_proposal

router = APIRouter()


@router.post("/api/llm/chart-proposal")
def chart_proposal(request: ChartProposalRequest) -> dict[str, object]:
    return {"proposal": openai_chart_proposal(request.context)}


@router.post("/api/llm/chat")
def agent_chat(request: AgentChatRequest) -> dict[str, object]:
    return openai_agent_chat(request)
