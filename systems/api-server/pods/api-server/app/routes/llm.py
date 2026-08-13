from fastapi import APIRouter, Depends, Query, Request

from app.auth.dependencies import auth_is_enabled, require_current_user
from app.auth.models import AuthenticatedUser
from app.contracts.chart import AgentChatRequest, ChartProposalRequest
from app.contracts.compare import CompanyCompareRequest
from app.contracts.related_indices import RelatedIndexCommentaryRequest
from app.services.ai_agents import openai_agent_chat, openai_chart_proposal, openai_related_index_commentary
from app.services.company_compare import (
    company_compare_analysis,
    company_compare_candidates,
    company_compare_quantitative,
)
from app.services.agent_rate_limit import enforce_agent_rate_limit

router = APIRouter()


@router.post("/api/llm/chart-proposal")
def chart_proposal(request: ChartProposalRequest, _user: AuthenticatedUser = Depends(require_current_user)) -> dict[str, object]:
    return {"proposal": openai_chart_proposal(request.context)}


@router.post("/api/llm/chat")
def agent_chat(request: AgentChatRequest, _user: AuthenticatedUser = Depends(require_current_user)) -> dict[str, object]:
    return openai_agent_chat(request)


@router.post("/api/llm/company-compare")
def company_compare(
    request: CompanyCompareRequest,
    http_request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, object]:
    if auth_is_enabled():
        enforce_agent_rate_limit(http_request.app, user.sub)
    return company_compare_analysis(request)


@router.post("/api/llm/company-compare/quantitative")
def compare_quantitative(
    request: CompanyCompareRequest,
    http_request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, object]:
    if auth_is_enabled():
        enforce_agent_rate_limit(http_request.app, user.sub)
    return company_compare_quantitative(request)


@router.get("/api/llm/company-compare/candidates")
def compare_candidates(
    http_request: Request,
    symbol: str = Query(min_length=1, max_length=10),
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, object]:
    if auth_is_enabled():
        enforce_agent_rate_limit(http_request.app, user.sub)
    return company_compare_candidates(symbol)


@router.post("/api/llm/related-index-commentary")
def related_index_commentary(
    request: RelatedIndexCommentaryRequest,
    http_request: Request,
    user: AuthenticatedUser = Depends(require_current_user),
) -> dict[str, object]:
    if auth_is_enabled():
        enforce_agent_rate_limit(http_request.app, user.sub)
    return openai_related_index_commentary(request)
