import { getChartDocumentForPanel, getChartDocumentId, type ChartRuntimeState } from "./runtime";
import type { ChartDocument } from "./types";
import type { PanelInstance } from "../layout/types";

export const DEFAULT_AGENT_DRAFT_SEED = "차트를 분석해줘";

export type AgentChartReference = {
  panelId: string;
  chartDocumentId: string;
  draftSeed?: string;
};

export type ResolvedAgentChartReference = {
  panel: PanelInstance;
  document: ChartDocument;
};

export function resolveAgentChartReference(
  panels: readonly PanelInstance[],
  runtime: ChartRuntimeState,
  reference?: AgentChartReference
): ResolvedAgentChartReference | null {
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

export function isAgentChartReferenceAvailable(
  panels: readonly PanelInstance[],
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
