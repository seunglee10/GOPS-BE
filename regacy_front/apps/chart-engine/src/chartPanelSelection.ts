import type { ChartRuntimePanel } from "./runtime";

export function findTargetChartPanel(
  panels: readonly ChartRuntimePanel[],
  selectedPanelId?: string
): ChartRuntimePanel | null {
  return panels.find((panel) => panel.type === "chart" && panel.id === selectedPanelId) ??
    panels.find((panel) => panel.type === "chart") ??
    null;
}
