import type { PanelType } from "../types/documents";

export interface PanelDefinition {
  type: PanelType;
  label: string;
  supportsTools: boolean;
}

export const defaultPanelRegistry: Record<PanelType, PanelDefinition> = {
  chart: { type: "chart", label: "Chart", supportsTools: true },
  chat: { type: "chat", label: "Chat", supportsTools: false },
  proposalList: { type: "proposalList", label: "AI Proposals", supportsTools: false }
};
