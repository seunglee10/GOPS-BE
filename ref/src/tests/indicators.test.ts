import { describe, expect, it } from "vitest";
import { calculateGraph, calculateSma } from "../calculations/indicators";
import { defaultIndicatorRegistry } from "../calculations/indicatorRegistry";
import type { Candle } from "../types/market";

function candles(count: number): Candle[] {
  return Array.from({ length: count }, (_, index) => {
    const close = 100 + index;
    return {
      symbol: "AAPL",
      timeframe: "1m",
      timestamp: `2026-01-01T00:${String(index).padStart(2, "0")}:00.000Z`,
      open: close - 0.5,
      high: close + 1,
      low: close - 1,
      close,
      volume: 1000 + index * 10,
      finalized: true
    };
  });
}

describe("indicators", () => {
  it("calculates SMA with null warmup points", () => {
    const output = calculateSma(candles(5), {
      id: "calc-sma",
      type: "SMA",
      inputs: { source: "close", period: 3 },
      outputKey: "sma"
    });

    expect(output.series[0].points[0].value).toBeNull();
    expect(output.series[0].points[2].value).toBe(101);
    expect(output.series[0].points[4].value).toBe(103);
  });

  it("calculates graph outputs from the indicator registry", () => {
    const outputs = calculateGraph(
      candles(40),
      {
        nodes: [
          { id: "calc-ema", type: "EMA", inputs: { source: "close", period: 10 }, outputKey: "ema" },
          { id: "calc-rsi", type: "RSI", inputs: { source: "close", period: 14 }, outputKey: "rsi" }
        ]
      },
      defaultIndicatorRegistry
    );

    expect(Object.keys(outputs)).toEqual(["calc-ema", "calc-rsi"]);
    const rsiPoints = outputs["calc-rsi"].series[0].points;
    expect(rsiPoints[rsiPoints.length - 1]?.value).toBeGreaterThan(50);
  });
});
