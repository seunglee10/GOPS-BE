import type { Candle } from "../types/market";
import type {
  CalculationGraph,
  CalculationInputs,
  CalculationNode,
  CalculationOutput,
  IndicatorPoint,
  IndicatorSeries
} from "../types/calculations";
import type { IndicatorRegistry } from "../types/calculations";

export const DEFAULT_INDICATOR_PRESETS = {
  SMA: { source: "close", period: 20 },
  EMA: { source: "close", period: 20 },
  RSI: { source: "close", period: 14 },
  MACD: { source: "close", fastPeriod: 12, slowPeriod: 26, signalPeriod: 9 },
  BOLLINGER_BANDS: { source: "close", period: 20, standardDeviation: 2 },
  VWAP: { reset: "session" },
  ATR: { period: 14 },
  VOLUME_MA: { period: 20 }
} as const;

export function calculateGraph(
  candles: Candle[],
  graph: CalculationGraph,
  registry: IndicatorRegistry
): Record<string, CalculationOutput> {
  const outputs: Record<string, CalculationOutput> = {};
  for (const node of graph.nodes) {
    const definition = registry[node.type];
    if (!definition) continue;
    const errors = definition.validateInputs(node.inputs);
    if (errors.length > 0) continue;
    outputs[node.id] = definition.calculate({ candles, node });
  }
  return outputs;
}

export function calculateSma(candles: Candle[], node: CalculationNode): CalculationOutput {
  const source = sourceKey(node.inputs.source);
  const period = numberInput(node.inputs.period, 20);
  const points = candles.map((candle, index) => {
    if (index + 1 < period) return point(candle.timestamp, null);
    const slice = candles.slice(index + 1 - period, index + 1);
    const average = sum(slice.map((item) => valueForSource(item, source))) / period;
    return point(candle.timestamp, round(average));
  });
  return output(node, [{ key: "sma", label: `SMA ${period}`, points, renderMode: "line" }]);
}

export function calculateEma(candles: Candle[], node: CalculationNode): CalculationOutput {
  const source = sourceKey(node.inputs.source);
  const period = numberInput(node.inputs.period, 20);
  const multiplier = 2 / (period + 1);
  let ema: number | null = null;
  const points = candles.map((candle, index) => {
    const value = valueForSource(candle, source);
    if (index + 1 < period) return point(candle.timestamp, null);
    if (ema === null) {
      const seed = candles.slice(index + 1 - period, index + 1).map((item) => valueForSource(item, source));
      ema = sum(seed) / period;
    } else {
      ema = value * multiplier + ema * (1 - multiplier);
    }
    return point(candle.timestamp, round(ema));
  });
  return output(node, [{ key: "ema", label: `EMA ${period}`, points, renderMode: "line" }]);
}

export function calculateRsi(candles: Candle[], node: CalculationNode): CalculationOutput {
  const period = numberInput(node.inputs.period, 14);
  let avgGain = 0;
  let avgLoss = 0;
  const points: IndicatorPoint[] = candles.map((candle, index) => {
    if (index === 0) return point(candle.timestamp, null);
    const change = candle.close - candles[index - 1].close;
    const gain = Math.max(0, change);
    const loss = Math.max(0, -change);
    if (index <= period) {
      avgGain += gain;
      avgLoss += loss;
      if (index < period) return point(candle.timestamp, null);
      avgGain /= period;
      avgLoss /= period;
    } else {
      avgGain = (avgGain * (period - 1) + gain) / period;
      avgLoss = (avgLoss * (period - 1) + loss) / period;
    }
    const rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
    const rsi = avgLoss === 0 ? 100 : 100 - 100 / (1 + rs);
    return point(candle.timestamp, round(rsi));
  });
  return output(node, [{ key: "rsi", label: `RSI ${period}`, points, renderMode: "line" }]);
}

export function calculateMacd(candles: Candle[], node: CalculationNode): CalculationOutput {
  const fastPeriod = numberInput(node.inputs.fastPeriod, 12);
  const slowPeriod = numberInput(node.inputs.slowPeriod, 26);
  const signalPeriod = numberInput(node.inputs.signalPeriod, 9);
  const fast = emaValues(candles.map((candle) => candle.close), fastPeriod);
  const slow = emaValues(candles.map((candle) => candle.close), slowPeriod);
  const macdValues = fast.map((value, index) => (value === null || slow[index] === null ? null : value - Number(slow[index])));
  const signalValues = emaNullableValues(macdValues, signalPeriod);
  const points = candles.map((candle, index) => {
    const macd = macdValues[index];
    const signal = signalValues[index];
    return {
      timestamp: candle.timestamp,
      value: macd === null ? null : round(macd),
      values: {
        macd: macd === null ? null : round(macd),
        signal: signal === null ? null : round(signal),
        histogram: macd === null || signal === null ? null : round(macd - signal)
      }
    };
  });
  return output(node, [
    { key: "macd", label: `MACD ${fastPeriod}/${slowPeriod}/${signalPeriod}`, points, renderMode: "line" }
  ]);
}

export function calculateBollingerBands(candles: Candle[], node: CalculationNode): CalculationOutput {
  const period = numberInput(node.inputs.period, 20);
  const deviation = numberInput(node.inputs.standardDeviation, 2);
  const points = candles.map((candle, index) => {
    if (index + 1 < period) return point(candle.timestamp, null, { upper: null, middle: null, lower: null });
    const values = candles.slice(index + 1 - period, index + 1).map((item) => item.close);
    const middle = sum(values) / period;
    const variance = sum(values.map((value) => (value - middle) ** 2)) / period;
    const width = Math.sqrt(variance) * deviation;
    return point(candle.timestamp, round(middle), {
      upper: round(middle + width),
      middle: round(middle),
      lower: round(middle - width)
    });
  });
  return output(node, [{ key: "bollinger", label: `Bollinger ${period}`, points, renderMode: "band" }]);
}

export function calculateVwap(candles: Candle[], node: CalculationNode): CalculationOutput {
  let priceVolume = 0;
  let volume = 0;
  const points = candles.map((candle) => {
    const typical = (candle.high + candle.low + candle.close) / 3;
    priceVolume += typical * candle.volume;
    volume += candle.volume;
    return point(candle.timestamp, volume > 0 ? round(priceVolume / volume) : null);
  });
  return output(node, [{ key: "vwap", label: "VWAP", points, renderMode: "line" }]);
}

export function calculateAtr(candles: Candle[], node: CalculationNode): CalculationOutput {
  const period = numberInput(node.inputs.period, 14);
  let atr: number | null = null;
  const trueRanges = candles.map((candle, index) => {
    if (index === 0) return candle.high - candle.low;
    const previousClose = candles[index - 1].close;
    return Math.max(candle.high - candle.low, Math.abs(candle.high - previousClose), Math.abs(candle.low - previousClose));
  });
  const points = candles.map((candle, index) => {
    if (index + 1 < period) return point(candle.timestamp, null);
    if (atr === null) {
      atr = sum(trueRanges.slice(index + 1 - period, index + 1)) / period;
    } else {
      atr = (atr * (period - 1) + trueRanges[index]) / period;
    }
    return point(candle.timestamp, round(atr));
  });
  return output(node, [{ key: "atr", label: `ATR ${period}`, points, renderMode: "line" }]);
}

export function calculateVolumeMa(candles: Candle[], node: CalculationNode): CalculationOutput {
  const period = numberInput(node.inputs.period, 20);
  const points = candles.map((candle, index) => {
    if (index + 1 < period) return point(candle.timestamp, null);
    const values = candles.slice(index + 1 - period, index + 1).map((item) => item.volume);
    return point(candle.timestamp, round(sum(values) / period));
  });
  return output(node, [{ key: "volume-ma", label: `Volume MA ${period}`, points, renderMode: "line" }]);
}

function output(node: CalculationNode, series: IndicatorSeries[]): CalculationOutput {
  return {
    nodeId: node.id,
    outputKey: node.outputKey,
    series,
    computedAt: new Date().toISOString()
  };
}

function point(timestamp: string, value: number | null, values?: Record<string, number | null>): IndicatorPoint {
  return { timestamp, value, values };
}

function valueForSource(candle: Candle, source: string): number {
  if (source === "open") return candle.open;
  if (source === "high") return candle.high;
  if (source === "low") return candle.low;
  if (source === "volume") return candle.volume;
  return candle.close;
}

function sourceKey(value: CalculationInputs[string]): "open" | "high" | "low" | "close" | "volume" {
  return value === "open" || value === "high" || value === "low" || value === "volume" ? value : "close";
}

function numberInput(value: CalculationInputs[string], fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function sum(values: number[]): number {
  return values.reduce((total, value) => total + value, 0);
}

function round(value: number): number {
  return Number(value.toFixed(6));
}

function emaValues(values: number[], period: number): Array<number | null> {
  const multiplier = 2 / (period + 1);
  let ema: number | null = null;
  return values.map((value, index) => {
    if (index + 1 < period) return null;
    if (ema === null) {
      ema = sum(values.slice(index + 1 - period, index + 1)) / period;
    } else {
      ema = value * multiplier + ema * (1 - multiplier);
    }
    return ema;
  });
}

function emaNullableValues(values: Array<number | null>, period: number): Array<number | null> {
  const result: Array<number | null> = [];
  const multiplier = 2 / (period + 1);
  let ema: number | null = null;
  let seed: number[] = [];
  for (const value of values) {
    if (value === null) {
      result.push(null);
      continue;
    }
    if (ema === null) {
      seed.push(value);
      if (seed.length < period) {
        result.push(null);
        continue;
      }
      ema = sum(seed) / period;
    } else {
      ema = value * multiplier + ema * (1 - multiplier);
    }
    result.push(ema);
  }
  return result;
}
