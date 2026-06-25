import type { ChartToolMode } from "../types/documents";

export interface ChartToolDefinition {
  mode: ChartToolMode;
  label: string;
}

export const defaultChartToolRegistry: Record<ChartToolMode, ChartToolDefinition> = {
  select: { mode: "select", label: "Select/Pan" },
  drawHorizontalLine: { mode: "drawHorizontalLine", label: "Horizontal Line" }
};
