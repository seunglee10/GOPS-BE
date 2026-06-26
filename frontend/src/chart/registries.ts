import type { ChartCommandType, ChartToolMode, DrawingEntity, DrawingType } from "./types";

export type DrawingDefinition = {
  type: DrawingType;
  label: string;
  minAnchors: number;
  maxAnchors: number;
  commandType: ChartCommandType;
};

export type ChartToolDefinition = {
  id: ChartToolMode;
  label: string;
  drawingType?: DrawingType;
};

export const drawingRegistry: Record<DrawingType, DrawingDefinition> = {
  horizontalLine: { type: "horizontalLine", label: "H-Line", minAnchors: 1, maxAnchors: 1, commandType: "chart.drawing.add" },
  trendLine: { type: "trendLine", label: "Trend", minAnchors: 2, maxAnchors: 2, commandType: "chart.drawing.add" },
  verticalMarker: { type: "verticalMarker", label: "Marker", minAnchors: 1, maxAnchors: 1, commandType: "chart.drawing.add" },
  textLabel: { type: "textLabel", label: "Text", minAnchors: 1, maxAnchors: 1, commandType: "chart.drawing.add" },
  pointMarker: { type: "pointMarker", label: "Point", minAnchors: 1, maxAnchors: 1, commandType: "chart.drawing.add" },
  arrow: { type: "arrow", label: "Arrow", minAnchors: 2, maxAnchors: 2, commandType: "chart.drawing.add" },
  rangeBox: { type: "rangeBox", label: "Range", minAnchors: 2, maxAnchors: 2, commandType: "chart.drawing.add" },
  measurement: { type: "measurement", label: "Measure", minAnchors: 2, maxAnchors: 2, commandType: "chart.measurement.add" },
  ellipse: { type: "ellipse", label: "Ellipse", minAnchors: 2, maxAnchors: 2, commandType: "chart.drawing.add" },
  riskRewardBox: { type: "riskRewardBox", label: "Risk", minAnchors: 3, maxAnchors: 3, commandType: "chart.drawing.add" },
  fibonacciRetracement: { type: "fibonacciRetracement", label: "Fibo", minAnchors: 2, maxAnchors: 2, commandType: "chart.drawing.add" }
};

export const chartToolRegistry: ChartToolDefinition[] = [
  { id: "select", label: "Select" },
  { id: "pan", label: "Pan" },
  { id: "draw-horizontalLine", label: "H-Line", drawingType: "horizontalLine" },
  { id: "draw-verticalMarker", label: "Marker", drawingType: "verticalMarker" },
  { id: "draw-trendLine", label: "Trend", drawingType: "trendLine" },
  { id: "draw-textLabel", label: "Text", drawingType: "textLabel" },
  { id: "draw-pointMarker", label: "Point", drawingType: "pointMarker" },
  { id: "draw-arrow", label: "Arrow", drawingType: "arrow" },
  { id: "draw-rangeBox", label: "Range", drawingType: "rangeBox" },
  { id: "draw-measurement", label: "Measure", drawingType: "measurement" }
];

export const commandRegistry = new Set<ChartCommandType>([
  "chart.symbol.set",
  "chart.timeframe.set",
  "chart.viewport.set",
  "chart.layer.visibility.set",
  "chart.undo",
  "chart.redo",
  "chart.drawing.add",
  "chart.drawing.update",
  "chart.drawing.remove",
  "chart.drawing.select",
  "chart.drawing.clearSelection",
  "chart.preview.set",
  "chart.preview.toggle",
  "chart.preview.apply",
  "chart.preview.clear",
  "chart.comparison.add",
  "chart.comparison.remove",
  "chart.comparison.update",
  "chart.measurement.add"
]);

export const rendererRegistry = [
  "background",
  "grid",
  "volume",
  "candles",
  "comparison",
  "movingAverage",
  "drawing",
  "preview",
  "crosshair",
  "axes"
] as const;

export function drawingNeedsTwoAnchors(type: DrawingType): boolean {
  return drawingRegistry[type]?.minAnchors > 1;
}

export function isSupportedDrawing(entity: DrawingEntity): boolean {
  const definition = drawingRegistry[entity.type];
  if (!definition || entity.anchors.length < definition.minAnchors || entity.anchors.length > definition.maxAnchors) {
    return false;
  }
  if (entity.type === "horizontalLine") {
    return hasAnchorValue(entity.anchors[0]);
  }
  if (entity.type === "verticalMarker") {
    return hasAnchorTime(entity.anchors[0]);
  }
  if (entity.type === "pointMarker" || entity.type === "textLabel") {
    return hasAnchorTime(entity.anchors[0]) && hasAnchorValue(entity.anchors[0]);
  }
  return entity.anchors.every((anchor) => hasAnchorTime(anchor) && hasAnchorValue(anchor));
}

function hasAnchorTime(anchor: DrawingEntity["anchors"][number] | undefined): boolean {
  return Boolean(anchor?.timestamp) || typeof anchor?.logicalIndex === "number";
}

function hasAnchorValue(anchor: DrawingEntity["anchors"][number] | undefined): boolean {
  return typeof anchor?.price === "number" || typeof anchor?.value === "number";
}
