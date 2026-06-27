import type { PanelInstance } from "../layout/types";

export function findTargetChartPanel(
  panels: readonly PanelInstance[],
  selectedPanelId?: string
): PanelInstance | null {
  return panels.find((panel) => panel.type === "chart" && panel.id === selectedPanelId) ??
    panels.find((panel) => panel.type === "chart") ??
    null;
}
