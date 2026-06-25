import { describe, expect, it } from "vitest";
import { applyBarEvent, applyEventBatch, applySnapshot, type CandlesBySymbol } from "../market/candleStore";
import type { BarEvent, Candle, MarketEventBatchMessage, MarketSnapshotMessage, UpdatedBarEvent } from "../types/market";

function candle(symbol: string, minute: number, close = 100 + minute, finalized = true): Candle {
  return {
    symbol,
    timeframe: "1m",
    timestamp: `2026-01-01T00:${String(minute).padStart(2, "0")}:00.000Z`,
    open: close - 0.5,
    high: close + 1,
    low: close - 1,
    close,
    volume: 1000 + minute,
    finalized
  };
}

function bar(type: "bar" | "updatedBar", symbol: string, minute: number, close: number): BarEvent | UpdatedBarEvent {
  return {
    type,
    provider: "dummy",
    symbol,
    timeframe: "1m",
    timestamp: candle(symbol, minute).timestamp,
    open: close - 0.5,
    high: close + 1,
    low: close - 1,
    close,
    volume: 2000 + minute,
    receivedAt: "2026-01-01T00:00:00.000Z"
  };
}

describe("candle store", () => {
  it("merges snapshots without dropping symbols outside the snapshot", () => {
    const current: CandlesBySymbol = {
      AAPL: [candle("AAPL", 1)],
      MSFT: [candle("MSFT", 1)]
    };
    const snapshot: MarketSnapshotMessage = {
      type: "snapshot",
      requestId: "request-1",
      provider: "dummy",
      timeframe: "1m",
      generatedAt: "2026-01-01T00:02:00.000Z",
      candlesBySymbol: {
        AAPL: [candle("AAPL", 2)]
      }
    };

    const next = applySnapshot(current, snapshot);

    expect(next.AAPL.map((item) => item.close)).toEqual([102]);
    expect(next.MSFT).toEqual(current.MSFT);
  });

  it("replaces same-timestamp live updates and appends newer finalized bars", () => {
    let store: CandlesBySymbol = { AAPL: [candle("AAPL", 1), candle("AAPL", 2, 102, false)] };

    store = applyBarEvent(store, bar("updatedBar", "AAPL", 2, 110));
    expect(store.AAPL).toHaveLength(2);
    expect(store.AAPL[1]).toMatchObject({ close: 110, finalized: false });

    store = applyBarEvent(store, bar("bar", "AAPL", 3, 111));
    expect(store.AAPL).toHaveLength(3);
    expect(store.AAPL[2]).toMatchObject({ close: 111, finalized: true });
  });

  it("ignores older bars so late events cannot rewrite the visible window", () => {
    const current: CandlesBySymbol = { AAPL: [candle("AAPL", 1), candle("AAPL", 2), candle("AAPL", 3)] };

    const next = applyBarEvent(current, bar("bar", "AAPL", 2, 999));

    expect(next).toBe(current);
    expect(next.AAPL.map((item) => item.close)).toEqual([101, 102, 103]);
  });

  it("applies event batches through the same bar replacement rules", () => {
    const batch: MarketEventBatchMessage = {
      type: "events",
      provider: "dummy",
      sequence: 1,
      sentAt: "2026-01-01T00:04:00.000Z",
      events: [bar("updatedBar", "AAPL", 2, 115), bar("bar", "AAPL", 3, 116)]
    };

    const next = applyEventBatch({ AAPL: [candle("AAPL", 1), candle("AAPL", 2)] }, batch);

    expect(next.AAPL.map((item) => item.close)).toEqual([101, 115, 116]);
  });
});
