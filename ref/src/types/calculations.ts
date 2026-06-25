import type { Candle, SymbolCode, Timeframe } from "./market";

export type CalculationNodeId = string;
export type IndicatorType =
  | "SMA"
  | "EMA"
  | "RSI"
  | "MACD"
  | "BOLLINGER_BANDS"
  | "VWAP"
  | "ATR"
  | "VOLUME_MA";

export type CalculationInputs = Record<string, string | number | boolean | string[]>;

export interface CalculationGraph {
  nodes: CalculationNode[];
}

export interface CalculationNode {
  id: CalculationNodeId;
  type: IndicatorType;
  inputs: CalculationInputs;
  outputKey: string;
}

export interface CalculationOutput {
  nodeId: CalculationNodeId;
  outputKey: string;
  series: IndicatorSeries[];
  computedAt: string;
}

export interface IndicatorSeries {
  key: string;
  label: string;
  points: IndicatorPoint[];
  renderMode: "line" | "histogram" | "band" | "cloud";
}

export interface IndicatorPoint {
  timestamp: string;
  value: number | null;
  values?: Record<string, number | null>;
}

export interface IndicatorCalculationInput {
  candles: Candle[];
  node: CalculationNode;
}

export interface IndicatorDefinition {
  type: IndicatorType;
  label: string;
  preferredPane: "price" | "volume" | "indicator";
  defaultInputs: CalculationInputs;
  validateInputs(inputs: CalculationInputs): import("./commands").CommandValidationError[];
  calculate(input: IndicatorCalculationInput): CalculationOutput;
}

export type IndicatorRegistry = Record<IndicatorType, IndicatorDefinition>;

export interface MarketSummary {
  symbol: SymbolCode;
  timeframe: Timeframe;
  latestPrice: number;
  latestTimestamp: string;
  changePercentFromFirstVisible: number;
  visibleChangeBaseTimestamp?: string;
  visibleChangeBaseClose?: number;
  liveChangePercent?: number;
  visibleHigh: number;
  visibleLow: number;
  visibleVolume: number;
  averageVolume: number;
  realizedVolatility: number;
  trend: "strong_up" | "up" | "sideways" | "down" | "strong_down" | "insufficient_data";
  notableSignals: MarketSignal[];
}

export interface MarketSignal {
  type:
    | "volume_spike"
    | "range_expansion"
    | "new_visible_high"
    | "new_visible_low"
    | "ema_cross"
    | "rsi_overbought"
    | "rsi_oversold";
  severity: "low" | "medium" | "high";
  message: string;
  timestamp: string;
}
