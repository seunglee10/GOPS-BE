import type { ChartDocument, ChartDocumentSnapshot } from "./types";

export function createChartDocument(id: string, symbol = "AAPL", timeframe = "1m"): ChartDocument {
  return {
    id,
    symbol,
    timeframe,
    viewport: {
      rightOffset: 0,
      visibleCount: 72
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
      grid: "#e3e3e3",
      text: "#2a2a2a",
      bullish: "#0f8a4b",
      bearish: "#b33a3a",
      ma5: "#2563eb",
      ma20: "#d97706",
      ma60: "#7c3aed",
      volume: "#9ca3af"
    },
    interactionState: {
      mode: "pan"
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
