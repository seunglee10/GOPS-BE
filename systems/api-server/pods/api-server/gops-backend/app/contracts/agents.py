import json
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


MAX_AGENT_REQUEST_BYTES = 64 * 1024
ShortString = Annotated[str, Field(max_length=128)]


class AgentChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=4000)


class CoachAnalysisRequest(BaseModel):
    """Public, lightweight request for a server-owned coach snapshot."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    selectedFillId: str | None = Field(default=None, min_length=1, max_length=128)
    tradingDate: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class AgentAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str | None = Field(default=None, max_length=32)
    intent: str = Field(default="analysis", min_length=1, max_length=4000)
    routerMode: str = Field(default="hybrid", max_length=32)
    agentIds: list[ShortString] = Field(default_factory=list, max_length=20)
    messages: list[AgentChatMessage] = Field(default_factory=list, max_length=50)
    chartContext: dict[str, Any] = Field(default_factory=dict)
    layoutContext: dict[str, Any] = Field(default_factory=dict)
    references: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    uiContext: dict[str, Any] = Field(default_factory=dict)
    marketEvents: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    chartProposal: dict[str, Any] | None = None
    coachRequest: CoachAnalysisRequest | None = None
    chartAction: str | None = Field(default=None, max_length=32)
    chartTargetSymbol: str | None = Field(default=None, max_length=32)
    chartPlacementIntent: str | None = Field(default=None, max_length=32)
    requestId: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    idempotencyKey: str | None = Field(default=None, max_length=128)
    userId: str | None = Field(default=None, max_length=128)
    mode: str | None = Field(default=None, max_length=32)
    analysisMode: str | None = Field(default=None, max_length=32)
    priority: str | None = Field(default=None, max_length=32)
    responseMode: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_total_request_size(self) -> "AgentAnalysisRequest":
        encoded = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_AGENT_REQUEST_BYTES:
            raise ValueError(f"agent request body exceeds {MAX_AGENT_REQUEST_BYTES} bytes")
        return self


class AgentLayoutResolveRequest(AgentAnalysisRequest):
    pass
