import { describe, expect, it } from "vitest";
import { createTimeScaleModel, zoomLogicalRangeAtAnchor } from "../renderer/timeScaleModel";
import { createValueScaleModel } from "../renderer/valueScaleModel";
import type { Candle } from "../types/market";

function candles(count: number): Candle[] {
  return Array.from({ length: count }, (_, index) => ({
    symbol: "AAPL",
    timeframe: "1m",
    timestamp: `2026-01-01T00:${String(index).padStart(2, "0")}:00.000Z`,
    open: 100,
    high: 101,
    low: 99,
    close: 100 + index,
    volume: 1000,
    finalized: true
  }));
}

describe("scale models", () => {
  it("round trips logical and x coordinates", () => {
    const scale = createTimeScaleModel({
      bounds: { x: 10, width: 500 },
      logicalRange: { from: 20, to: 119 },
      visibleDataRange: { from: 20, toExclusive: 120 },
      visibleCandles: candles(100)
    });

    const x = scale.logicalToX(55);

    expect(scale.xToLogical(x)).toBeCloseTo(55, 6);
    expect(scale.timestampToLogical("2026-01-01T00:35:00.000Z")).toBe(55);
    expect(scale.timestampToX("2026-01-01T00:35:00.000Z")).toBeCloseTo(x, 6);
  });

  it("keeps the zoom anchor stable", () => {
    const current = { from: 20, to: 119 };
    const anchor = 70;
    const next = zoomLogicalRangeAtAnchor(current, anchor, 0.5, { minBars: 20, maxBars: 1000 }, 200);
    const currentRatio = (anchor - current.from) / (current.to - current.from + 1);
    const nextRatio = (anchor - next.from) / (next.to - next.from + 1);

    expect(next.to - next.from + 1).toBeCloseTo(50, 6);
    expect(nextRatio).toBeCloseTo(currentRatio, 6);
  });

  it("round trips value and y coordinates with formatted ticks", () => {
    const scale = createValueScaleModel({
      scaleId: "scale-price-right",
      mode: "price",
      domain: [90, 120],
      bounds: { y: 20, height: 300 }
    });

    const y = scale.valueToY(105);

    expect(scale.yToValue(y)).toBeCloseTo(105, 6);
    expect(scale.ticks).toHaveLength(4);
    expect(scale.ticks[0].label).toBe("120.0");
  });
});
