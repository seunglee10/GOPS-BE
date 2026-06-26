import type { CandleData, CandleEvent, CandleSnapshot } from "./types";

export type CandleMergeResult = {
  candles: CandleData[];
  applied: boolean;
  message: string;
};

export function candleKey(symbol: string, interval: string): string {
  return `${symbol.toUpperCase()}::${interval}`;
}

export function applySnapshotToCandles(snapshot: CandleSnapshot): CandleData[] {
  return [...snapshot.candles].sort(compareCandles);
}

export function applyCandleEvent(current: CandleData[], event: CandleEvent): CandleMergeResult {
  const incomingTime = Date.parse(event.data.timestamp);
  if (!Number.isFinite(incomingTime)) {
    return { candles: current, applied: false, message: "Incoming candle timestamp is invalid." };
  }

  const candles = [...current].sort(compareCandles);
  const lastTime = candles.length ? Date.parse(candles[candles.length - 1].timestamp) : Number.NEGATIVE_INFINITY;
  const index = candles.findIndex((candle) => candle.timestamp === event.data.timestamp);

  if (index < 0 && incomingTime < lastTime) {
    return { candles, applied: false, message: "Stale candle event ignored." };
  }

  if (index >= 0) {
    candles[index] = event.data;
    return {
      candles: candles.sort(compareCandles),
      applied: true,
      message: event.type === "CANDLE_CORRECTED" ? "Corrected candle replaced." : "Candle updated."
    };
  }

  candles.push(event.data);
  return { candles: candles.sort(compareCandles), applied: true, message: "Candle appended." };
}

export function compareCandles(left: CandleData, right: CandleData): number {
  return Date.parse(left.timestamp) - Date.parse(right.timestamp);
}
