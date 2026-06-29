from typing import Any

from pydantic import BaseModel, Field


class AgentAnalysisRequest(BaseModel):
    symbol: str | None = None
    intent: str = "analysis"
    agentIds: list[str] = Field(default_factory=list)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    chartContext: dict[str, Any] = Field(default_factory=dict)
    marketEvents: list[dict[str, Any]] = Field(default_factory=list)
    chartProposal: dict[str, Any] | None = None
