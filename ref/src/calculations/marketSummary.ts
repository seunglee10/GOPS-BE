import type { CalculationOutput, MarketSignal, MarketSummary } from "../types/calculations";
import type { Candle, SymbolCode, Timeframe } from "../types/market";

export function summarizeMarket(
  symbol: SymbolCode,
  timeframe: Timeframe,
  candles: Candle[],
  calculationOutputs: Record<string, CalculationOutput> = {}
): MarketSummary {
  if (candles.length < 2) {
    const latest = candles[candles.length - 1];
    const latestPrice = latest?.close ?? 0;
    return {
      symbol,
      timeframe,
      latestPrice,
      latestTimestamp: latest?.timestamp ?? "",
      changePercentFromFirstVisible: 0,
      visibleHigh: latest?.high ?? latestPrice,
      visibleLow: latest?.low ?? latestPrice,
      visibleVolume: latest?.volume ?? 0,
      averageVolume: latest?.volume ?? 0,
      realizedVolatility: 0,
      trend: "insufficient_data",
      notableSignals: []
    };
  }

  const first = candles[0];
  const latest = candles[candles.length - 1];
  const visibleHigh = Math.max(...candles.map((candle) => candle.high));
  const visibleLow = Math.min(...candles.map((candle) => candle.low));
  const visibleVolume = candles.reduce((total, candle) => total + candle.volume, 0);
  const averageVolume = visibleVolume / candles.length;
  const changePercentFromFirstVisible = ((latest.close - first.close) / first.close) * 100;
  const returns = candles.slice(1).map((candle, index) => Math.log(candle.close / candles[index].close));
  const realizedVolatility = Math.sqrt(returns.reduce((total, value) => total + value ** 2, 0) / returns.length) * 100;
  const trend = classifyTrend(changePercentFromFirstVisible);
  const notableSignals = collectSignals(symbol, candles, averageVolume, visibleHigh, visibleLow, calculationOutputs);

  return {
    symbol,
    timeframe,
    latestPrice: latest.close,
    latestTimestamp: latest.timestamp,
    changePercentFromFirstVisible: round(changePercentFromFirstVisible),
    visibleChangeBaseTimestamp: first.timestamp,
    visibleChangeBaseClose: first.close,
    visibleHigh,
    visibleLow,
    visibleVolume,
    averageVolume,
    realizedVolatility: round(realizedVolatility),
    trend,
    notableSignals
  };
}

function classifyTrend(changePercent: number): MarketSummary["trend"] {
  if (changePercent >= 2.5) return "strong_up";
  if (changePercent >= 0.45) return "up";
  if (changePercent <= -2.5) return "strong_down";
  if (changePercent <= -0.45) return "down";
  return "sideways";
}

function collectSignals(
  symbol: SymbolCode,
  candles: Candle[],
  averageVolume: number,
  visibleHigh: number,
  visibleLow: number,
  calculationOutputs: Record<string, CalculationOutput>
): MarketSignal[] {
  const latest = candles[candles.length - 1];
  const signals: MarketSignal[] = [];
  if (latest.volume > averageVolume * 1.8) {
    signals.push({
      type: "volume_spike",
      severity: latest.volume > averageVolume * 2.5 ? "high" : "medium",
      message: `${symbol} volume is elevated versus the visible average.`,
      timestamp: latest.timestamp
    });
  }
  if (latest.high >= visibleHigh) {
    signals.push({
      type: "new_visible_high",
      severity: "medium",
      message: `${symbol} is pressing the visible-range high.`,
      timestamp: latest.timestamp
    });
  }
  if (latest.low <= visibleLow) {
    signals.push({
      type: "new_visible_low",
      severity: "medium",
      message: `${symbol} is testing the visible-range low.`,
      timestamp: latest.timestamp
    });
  }

  for (const output of Object.values(calculationOutputs)) {
    const rsi = output.series.find((series) => series.key === "rsi");
    const lastRsi = rsi?.points[rsi.points.length - 1]?.value;
    if (typeof lastRsi === "number" && lastRsi >= 70) {
      signals.push({
        type: "rsi_overbought",
        severity: "medium",
        message: `${symbol} RSI is above 70.`,
        timestamp: latest.timestamp
      });
    }
    if (typeof lastRsi === "number" && lastRsi <= 30) {
      signals.push({
        type: "rsi_oversold",
        severity: "medium",
        message: `${symbol} RSI is below 30.`,
        timestamp: latest.timestamp
      });
    }
  }

  return signals.slice(0, 6);
}

function round(value: number): number {
  return Number(value.toFixed(4));
}
