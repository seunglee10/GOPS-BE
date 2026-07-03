import type { ChartAction, ChartState, DrawingEntity } from "./types";
import { defaultVisibleBarsForInterval } from "./types";
import { latestCandleRightOffset, normalizeViewport } from "./viewport";

export function applyChartAction(chart: ChartState, action: ChartAction): ChartState {
  switch (action.type) {
    case "setSymbol": {
      const symbol = action.symbol.toUpperCase();
      return {
        ...chart,
        symbol,
        rightOffset: latestCandleRightOffset(chart.visibleCount),
        selectedDrawingId: undefined,
        drawings: chart.drawings.filter((drawing) => drawing.anchors.every((anchor) => !anchor.symbol || anchor.symbol === symbol))
      };
    }
    case "setInterval":
      return {
        ...chart,
        interval: action.interval,
        visibleCount: defaultVisibleBarsForInterval(action.interval),
        rightOffset: latestCandleRightOffset(defaultVisibleBarsForInterval(action.interval)),
        selectedDrawingId: undefined
      };
    case "setTool":
      return {
        ...chart,
        toolMode: action.toolMode,
        selectedDrawingId: action.toolMode === "select" ? chart.selectedDrawingId : undefined
      };
    case "toggleLayer":
      return {
        ...chart,
        layers: { ...chart.layers, [action.layer]: !chart.layers[action.layer] }
      };
    case "setLayer":
      return {
        ...chart,
        layers: { ...chart.layers, [action.layer]: action.enabled }
      };
    case "setVolumeRatio":
      return {
        ...chart,
        volumeRatio: Math.max(0.1, Math.min(0.45, action.ratio))
      };
    case "setViewport": {
      const viewport = normalizeViewport(
        { visibleCount: action.visibleCount, rightOffset: action.rightOffset },
        chart.candles.length
      );
      return { ...chart, ...viewport };
    }
    case "addDrawing":
      return {
        ...chart,
        drawings: upsertDrawing(chart.drawings, action.drawing),
        selectedDrawingId: action.drawing.id
      };
    case "updateDrawing":
      return {
        ...chart,
        drawings: chart.drawings.map((drawing) => (
          drawing.id === action.drawingId
            ? { ...drawing, ...action.patch, updatedAt: new Date().toISOString() }
            : drawing
        )),
        selectedDrawingId: action.drawingId
      };
    case "deleteDrawing":
      return {
        ...chart,
        drawings: chart.drawings.filter((drawing) => drawing.id !== action.drawingId),
        selectedDrawingId: chart.selectedDrawingId === action.drawingId ? undefined : chart.selectedDrawingId
      };
    case "selectDrawing":
      return { ...chart, selectedDrawingId: action.drawingId };
    case "clearDrawings":
      return { ...chart, drawings: [], selectedDrawingId: undefined };
    default:
      return chart;
  }
}

export function applyChartActions(chart: ChartState, actions: ChartAction[]): ChartState {
  return actions.reduce(applyChartAction, chart);
}

function upsertDrawing(drawings: DrawingEntity[], drawing: DrawingEntity): DrawingEntity[] {
  const index = drawings.findIndex((item) => item.id === drawing.id);
  if (index < 0) {
    return [...drawings, drawing];
  }
  const next = [...drawings];
  next[index] = drawing;
  return next;
}
