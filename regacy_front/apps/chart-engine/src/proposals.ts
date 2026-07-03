import { chartCapabilities } from "./capabilities";
import { makeChartCommand } from "./commands";
import { normalizeSupportedSymbol } from "./symbols";
import type { CandleData, ChartCommand, ChartCommandType, ChartDataStatus, ChartDocument, ChartProposal, RenderScene, StreamStatus } from "./types";

const proposalCommandTypes: ChartCommandType[] = [
  "chart.symbol.set",
  "chart.timeframe.set",
  "chart.viewport.set",
  "chart.layer.visibility.set",
  "chart.drawing.add",
  "chart.drawing.update",
  "chart.drawing.remove",
  "chart.drawing.select",
  "chart.drawing.clearSelection",
  "chart.comparison.add",
  "chart.comparison.remove",
  "chart.comparison.update",
  "chart.measurement.add"
];

export type ChartProposalRequestContext = {
  panelId: string;
  chartDocument: Pick<ChartDocument, "id" | "symbol" | "timeframe" | "viewport" | "layers" | "drawings" | "comparisons">;
  visibleSummary: {
    high?: string;
    low?: string;
    change?: string;
    lastPrice?: string;
  };
  dataStatus: {
    state: ChartDataStatus["state"];
    message?: string;
    backfillStatus?: ChartDataStatus["backfillStatus"];
    canBackfill?: boolean;
    candleCount: number;
    hasVisibleCandles: boolean;
  };
  streamStatus: StreamStatus;
  supportedSymbols: readonly string[];
  capabilities: typeof chartCapabilities;
};

export function buildChartProposalRequest({
  panelId,
  document,
  scene,
  streamStatus,
  symbolUniverse
}: {
  panelId: string;
  document: ChartDocument;
  scene?: RenderScene;
  streamStatus: StreamStatus;
  symbolUniverse?: readonly string[];
}): ChartProposalRequestContext {
  return {
    panelId,
    chartDocument: {
      id: document.id,
      symbol: document.symbol,
      timeframe: document.timeframe,
      viewport: document.viewport,
      layers: document.layers,
      drawings: document.drawings,
      comparisons: document.comparisons
    },
    visibleSummary: {
      high: scene?.labels.visibleHigh,
      low: scene?.labels.visibleLow,
      change: scene?.labels.change,
      lastPrice: scene?.labels.lastPrice
    },
    dataStatus: {
      state: scene?.state === "ready" || scene?.state === "partial" || scene?.state === "empty" || scene?.state === "error" ? scene.state : "loading",
      message: scene?.message,
      candleCount: scene?.candles.length ?? 0,
      hasVisibleCandles: Boolean(scene?.candles.length)
    },
    streamStatus,
    supportedSymbols: normalizeSymbolUniverse(symbolUniverse),
    capabilities: chartCapabilities
  };
}

export function buildChartAgentContext({
  panelId,
  document,
  candles,
  dataStatus,
  streamStatus,
  symbolUniverse
}: {
  panelId: string;
  document: ChartDocument;
  candles: CandleData[];
  dataStatus?: ChartDataStatus;
  streamStatus: StreamStatus;
  symbolUniverse?: readonly string[];
}): ChartProposalRequestContext {
  const visibleCount = Math.min(document.viewport.visibleCount, candles.length);
  const rightOffset = Math.min(document.viewport.rightOffset, Math.max(0, candles.length - 1));
  const end = Math.max(0, candles.length - rightOffset);
  const visibleCandles = candles.slice(Math.max(0, end - visibleCount), end);
  const first = visibleCandles[0];
  const last = visibleCandles[visibleCandles.length - 1];
  const highs = visibleCandles.map((candle) => candle.high);
  const lows = visibleCandles.map((candle) => candle.low);
  const change = first && last ? ((last.close - first.open) / Math.max(0.0001, first.open)) * 100 : undefined;

  return {
    panelId,
    chartDocument: {
      id: document.id,
      symbol: document.symbol,
      timeframe: document.timeframe,
      viewport: document.viewport,
      layers: document.layers,
      drawings: document.drawings,
      comparisons: document.comparisons
    },
    visibleSummary: {
      high: highs.length ? Math.max(...highs).toFixed(2) : undefined,
      low: lows.length ? Math.min(...lows).toFixed(2) : undefined,
      change: typeof change === "number" ? `${change >= 0 ? "+" : ""}${change.toFixed(2)}%` : undefined,
      lastPrice: last ? last.close.toFixed(2) : undefined
    },
    dataStatus: {
      state: dataStatus?.state ?? (candles.length > 0 ? "ready" : "loading"),
      message: dataStatus?.message,
      backfillStatus: dataStatus?.backfillStatus,
      canBackfill: dataStatus?.canBackfill,
      candleCount: candles.length,
      hasVisibleCandles: visibleCandles.length > 0
    },
    streamStatus,
    supportedSymbols: normalizeSymbolUniverse(symbolUniverse),
    capabilities: chartCapabilities
  };
}

function normalizeSymbolUniverse(symbolUniverse?: readonly string[]): string[] {
  const normalized = (symbolUniverse ?? [])
    .map((symbol) => normalizeSupportedSymbol(symbol))
    .filter((symbol): symbol is string => Boolean(symbol));

  return Array.from(new Set(normalized)).slice(0, 100);
}

export function normalizeChartProposal(payload: unknown, target: { panelId: string; chartDocumentId: string }): ChartProposal {
  const source = readObject((readObject(payload)?.proposal ?? payload));
  if (!source) {
    throw new Error("Chart proposal payload is invalid.");
  }

  const title = readString(source.title) ?? "Chart proposal";
  const rationale = readString(source.rationale) ?? "Generated chart adjustment.";
  const summary = readString(source.summary) ?? title;
  const createdByAgentId = readString(source.createdByAgentId) ?? "agent-01";
  const commandPayloads = Array.isArray(source.commands) ? source.commands : [];
  const commands = commandPayloads
    .map((item) => normalizeProposalCommand(item, target))
    .filter((command): command is ChartCommand => Boolean(command));

  if (commands.length === 0) {
    throw new Error("Chart proposal did not include any valid chart commands.");
  }

  return {
    id: readString(source.id) ?? `chart-proposal-${crypto.randomUUID()}`,
    title,
    rationale,
    summary,
    target,
    commands,
    insights: Array.isArray(source.insights)
      ? source.insights.filter((item): item is string => typeof item === "string" && item.trim().length > 0).slice(0, 5)
      : [],
    status: "pending",
    createdAt: readString(source.createdAt) ?? new Date().toISOString(),
    createdByAgentId
  };
}

function normalizeProposalCommand(value: unknown, target: { panelId: string; chartDocumentId: string }) {
  const source = readObject(value);
  const type = readCommandType(source?.type);
  if (!source || !type) {
    return null;
  }

  const payload = readObject(source.payload) ?? {};
  const historyScope = type === "chart.symbol.set" ? "external" : undefined;
  return makeChartCommand(type, "llm", target, payload, undefined, historyScope);
}

function readCommandType(value: unknown): ChartCommandType | null {
  return proposalCommandTypes.includes(value as ChartCommandType) ? (value as ChartCommandType) : null;
}

function readObject(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}
