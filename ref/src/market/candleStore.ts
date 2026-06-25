import type { BarEvent, Candle, MarketEventBatchMessage, MarketSnapshotMessage, SymbolCode, UpdatedBarEvent } from "../types/market";

export type CandlesBySymbol = Record<SymbolCode, Candle[]>;

export function applySnapshot(current: CandlesBySymbol, message: MarketSnapshotMessage): CandlesBySymbol {
  const next: CandlesBySymbol = { ...current };
  for (const [symbol, candles] of Object.entries(message.candlesBySymbol)) {
    next[symbol] = normalizeCandles(candles);
  }
  return next;
}

export function applyEventBatch(current: CandlesBySymbol, message: MarketEventBatchMessage): CandlesBySymbol {
  let next = current;
  for (const event of message.events) {
    if (event.type !== "bar" && event.type !== "updatedBar") continue;
    next = applyBarEvent(next, event);
  }
  return next;
}

export function applyBarEvent(current: CandlesBySymbol, event: BarEvent | UpdatedBarEvent): CandlesBySymbol {
  const existing = current[event.symbol] ?? [];
  const candle = candleFromBarEvent(event);
  const last = existing[existing.length - 1];
  if (last && candle.timestamp < last.timestamp) {
    return current;
  }
  const index = existing.findIndex((item) => item.timestamp === candle.timestamp && item.timeframe === candle.timeframe);
  let candles: Candle[];
  if (index >= 0) {
    candles = [...existing.slice(0, index), candle, ...existing.slice(index + 1)];
  } else {
    candles = [...existing, candle];
  }
  return {
    ...current,
    [event.symbol]: normalizeCandles(candles).slice(-1200)
  };
}

function normalizeCandles(candles: Candle[]): Candle[] {
  const byTimestamp = new Map<string, Candle>();
  for (const candle of candles) {
    byTimestamp.set(`${candle.timeframe}:${candle.timestamp}`, candle);
  }
  return Array.from(byTimestamp.values()).sort((a, b) => a.timestamp.localeCompare(b.timestamp));
}

function candleFromBarEvent(event: BarEvent | UpdatedBarEvent): Candle {
  return {
    symbol: event.symbol,
    timeframe: event.timeframe,
    timestamp: event.timestamp,
    open: event.open,
    high: event.high,
    low: event.low,
    close: event.close,
    volume: event.volume,
    vwap: event.vwap,
    tradeCount: event.tradeCount,
    finalized: event.type === "bar"
  };
}
