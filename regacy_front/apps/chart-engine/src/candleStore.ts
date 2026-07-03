import type { CandleData, CandleEvent, CandleSnapshot } from "./types";
import { canonicalTimestamp } from "./time";

export type CandleMergeResult = {
  candles: CandleData[];
  applied: boolean;
  message: string;
};

export function candleKey(symbol: string, interval: string): string {
  return `${symbol.toUpperCase()}::${interval}`;
}

export function applySnapshotToCandles(snapshot: CandleSnapshot, current: CandleData[] = []): CandleData[] {
  const byTimestamp = new Map<string, CandleData>();
  [...current, ...snapshot.candles].forEach((candle) => {
    const key = candleTimestampKey(candle);
    if (key) {
      byTimestamp.set(key, { ...candle, timestamp: key });
    }
  });
  return [...byTimestamp.values()].sort(compareCandles);
}

export function applyCandleEvent(current: CandleData[], event: CandleEvent): CandleMergeResult {
  const incomingKey = candleTimestampKey(event.data);
  const incomingTime = incomingKey ? Date.parse(incomingKey) : Number.NaN;
  if (!incomingKey || !Number.isFinite(incomingTime)) {
    return { candles: current, applied: false, message: "Incoming candle timestamp is invalid." };
  }

  const candles = [...current].sort(compareCandles);
  const lastTime = candles.length ? Date.parse(candles[candles.length - 1].timestamp) : Number.NEGATIVE_INFINITY;
  const index = candles.findIndex((candle) => candleTimestampKey(candle) === incomingKey);
  const incomingCandle = { ...event.data, timestamp: incomingKey };

  if (index < 0 && incomingTime < lastTime) {
    return { candles, applied: false, message: "Stale candle event ignored." };
  }

  if (index >= 0) {
    candles[index] = incomingCandle;
    return {
      candles: candles.sort(compareCandles),
      applied: true,
      message: event.type === "CANDLE_CORRECTED" ? "Corrected candle replaced." : "Candle updated."
    };
  }

  candles.push(incomingCandle);
  return { candles: candles.sort(compareCandles), applied: true, message: "Candle appended." };
}

export function compareCandles(left: CandleData, right: CandleData): number {
  return Date.parse(left.timestamp) - Date.parse(right.timestamp);
}

function candleTimestampKey(candle: CandleData): string | null {
  return canonicalTimestamp(candle.timestamp);
}
