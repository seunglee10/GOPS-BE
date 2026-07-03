import { defaultVisibleBarsForInterval, normalizeChartInterval, type ChartInterval } from "./intervals";
import type { ChartDocument, ChartDocumentSnapshot } from "./types";
import { DEFAULT_CHART_SYMBOL } from "./symbols";

export function createChartDocument(id: string, symbol = DEFAULT_CHART_SYMBOL, timeframe: ChartInterval | string = "1m"): ChartDocument {
  const resolvedTimeframe = normalizeChartInterval(timeframe) ?? "1m";
  return {
    id,
    symbol,
    timeframe: resolvedTimeframe,
    viewport: {
      rightOffset: 0,
      visibleCount: defaultVisibleBarsForInterval(resolvedTimeframe)
    },
    panes: [
      { id: "price", heightRatio: 0.74 },
      { id: "volume", heightRatio: 0.26 }
    ],
    layers: {
      candles: true,
      volume: true,
      ma5: true,
      ma20: true,
      ma60: true
    },
    style: {
      background: "#ffffff",
      grid: "#edf1f7",
      text: "#667085",
      bullish: "#16a86b",
      bearish: "#e94b5b",
      ma5: "#2f6fed",
      ma20: "#c58a1f",
      ma60: "#7b61d1",
      volume: "#9ca3af"
    },
    interactionState: {
      mode: "pan",
      trendLineExtension: "segment"
    },
    drawings: [],
    comparisons: [],
    history: [],
    future: [],
    updatedAt: new Date().toISOString()
  };
}

export function cloneChartDocument(document: ChartDocument): ChartDocument {
  return structuredClone(document) as ChartDocument;
}

export function snapshotChartDocument(document: ChartDocument): ChartDocumentSnapshot {
  return {
    id: document.id,
    symbol: document.symbol,
    timeframe: document.timeframe,
    viewport: { ...document.viewport },
    panes: structuredClone(document.panes) as ChartDocument["panes"],
    layers: { ...document.layers },
    style: { ...document.style },
    interactionState: { ...document.interactionState },
    drawings: structuredClone(document.drawings) as ChartDocument["drawings"],
    comparisons: structuredClone(document.comparisons) as ChartDocument["comparisons"],
    selectedDrawingId: document.selectedDrawingId,
    updatedAt: document.updatedAt
  };
}

export function restoreChartDocumentSnapshot(
  current: ChartDocument,
  snapshot: ChartDocumentSnapshot
): ChartDocument {
  return {
    ...current,
    symbol: snapshot.symbol,
    timeframe: snapshot.timeframe,
    viewport: { ...snapshot.viewport },
    panes: structuredClone(snapshot.panes) as ChartDocument["panes"],
    layers: { ...snapshot.layers },
    style: { ...snapshot.style },
    interactionState: { ...snapshot.interactionState },
    drawings: structuredClone(snapshot.drawings) as ChartDocument["drawings"],
    comparisons: structuredClone(snapshot.comparisons) as ChartDocument["comparisons"],
    selectedDrawingId: snapshot.selectedDrawingId,
    updatedAt: new Date().toISOString()
  };
}
