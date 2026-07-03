export type AgentAccessAgent = {
  id: string;
};

export type ChartAgentAccess = {
  enabled: boolean;
  reason: "agent-01" | "no-chart-agent" | "orchestration";
};

export function getChartAgentAccess(selectedAgents: AgentAccessAgent[]): ChartAgentAccess {
  if (selectedAgents.length === 1 && selectedAgents[0]?.id === "agent-01") {
    return { enabled: true, reason: "agent-01" };
  }
  if (selectedAgents.length > 1) {
    return { enabled: false, reason: "orchestration" };
  }
  return { enabled: false, reason: "no-chart-agent" };
}
