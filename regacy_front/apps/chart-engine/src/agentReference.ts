import { getChartDocumentForPanel, getChartDocumentId, type ChartRuntimeState } from "./runtime";
import type { ChartDocument } from "./types";

export const DEFAULT_AGENT_DRAFT_SEED = "차트를 분석해줘";

export type AgentChartReference = {
  panelId: string;
  chartDocumentId: string;
  draftSeed?: string;
};

export type ResolvedAgentChartReference<Panel> = {
  panel: Panel;
  document: ChartDocument;
};

export function resolveAgentChartReference<Panel extends { id: string; type: string; props: Record<string, unknown>; chartDocumentId?: string }>(
  panels: readonly Panel[],
  runtime: ChartRuntimeState,
  reference?: AgentChartReference
): ResolvedAgentChartReference<Panel> | null {
  if (!reference) {
    return null;
  }

  const panel = panels.find((item) => item.id === reference.panelId && item.type === "chart");
  if (!panel || getChartDocumentId(panel) !== reference.chartDocumentId) {
    return null;
  }

  return {
    panel,
    document: getChartDocumentForPanel(runtime, panel)
  };
}

export function isAgentChartReferenceAvailable<Panel extends { id: string; type: string; props: Record<string, unknown>; chartDocumentId?: string }>(
  panels: readonly Panel[],
  reference?: AgentChartReference
): boolean {
  if (!reference) {
    return false;
  }

  return panels.some((panel) =>
    panel.id === reference.panelId &&
    panel.type === "chart" &&
    getChartDocumentId(panel) === reference.chartDocumentId
  );
}

export function resolveAgentSendContent(draft: string, seed = DEFAULT_AGENT_DRAFT_SEED): string {
  return draft.trim() || seed;
}
