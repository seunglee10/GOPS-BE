from typing import Any

from pydantic import BaseModel

from gops_agents.chart_command.schemas import chart_command_payload_schema, chart_command_schema, filled_command_payload


class ChartProposalRequest(BaseModel):
    context: dict[str, Any]


class AgentChatMessage(BaseModel):
    role: str
    content: str


class AgentChatRequest(BaseModel):
    agentIds: list[str] = []
    messages: list[AgentChatMessage]
    context: dict[str, Any]
