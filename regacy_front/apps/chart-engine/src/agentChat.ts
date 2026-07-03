import { normalizeChartProposal } from "./proposals";
import type { ChartProposal } from "./types";

export type AgentChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt: string;
};

export type AgentChatResponse = {
  reply: string;
  proposal?: ChartProposal;
};

export function createChatMessage(role: AgentChatMessage["role"], content: string): AgentChatMessage {
  return {
    id: `agent-chat-${crypto.randomUUID()}`,
    role,
    content,
    createdAt: new Date().toISOString()
  };
}

export function normalizeAgentChatResponse(
  payload: unknown,
  target: { panelId: string; chartDocumentId: string }
): AgentChatResponse {
  const source = readObject(payload);
  if (!source) {
    throw new Error("Agent chat payload is invalid.");
  }

  const reply = readString(source.reply) ?? "I prepared a chart response.";
  const commands = Array.isArray(source.commands) ? source.commands : [];
  if (commands.length === 0) {
    return { reply };
  }

  const proposal = normalizeChartProposal({
    id: readString(source.id),
    title: readString(source.title) ?? "Agent 01 chart action",
    summary: readString(source.summary) ?? reply,
    rationale: readString(source.rationale) ?? "Agent 01 mapped the request to chart commands.",
    createdByAgentId: readString(source.createdByAgentId) ?? "agent-01",
    insights: Array.isArray(source.insights) ? source.insights : [],
    commands
  }, target);

  return { reply, proposal };
}

function readObject(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}
