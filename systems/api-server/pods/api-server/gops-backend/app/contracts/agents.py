from typing import Any

from pydantic import BaseModel, Field


class AgentAnalysisRequest(BaseModel):
    symbol: str | None = None
    intent: str = "analysis"
    routerMode: str = "hybrid"
    agentIds: list[str] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    chartContext: dict[str, Any] = Field(default_factory=dict)
    layoutContext: dict[str, Any] = Field(default_factory=dict)
    marketEvents: list[dict[str, Any]] = Field(default_factory=list)
    chartProposal: dict[str, Any] | None = None
    requestId: str | None = None
    idempotencyKey: str | None = None
    userId: str | None = None
    mode: str | None = None
    analysisMode: str | None = None
    priority: str | None = None
    responseMode: str | None = None
