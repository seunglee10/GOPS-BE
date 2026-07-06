from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str | None = None
    intent: str = "analysis"
    routerMode: str = "hybrid"
    agentIds: list[str] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    chartContext: dict[str, Any] = Field(default_factory=dict)
    layoutContext: dict[str, Any] = Field(default_factory=dict)
    references: list[dict[str, Any]] = Field(default_factory=list)
    uiContext: dict[str, Any] = Field(default_factory=dict)
    marketEvents: list[dict[str, Any]] = Field(default_factory=list)
    chartProposal: dict[str, Any] | None = None
    chartAction: str | None = None
    chartTargetSymbol: str | None = None
    chartPlacementIntent: str | None = None
    requestId: str | None = None
    idempotencyKey: str | None = None
    userId: str | None = None
    mode: str | None = None
    analysisMode: str | None = None
    priority: str | None = None
    responseMode: str | None = None


class AgentLayoutResolveRequest(AgentAnalysisRequest):
    pass
