import { describe, expect, it } from "vitest";
import { defaultLayerRendererRegistry } from "../renderer/layerRendererRegistry";
import { buildRenderScene } from "../renderer/sceneBuilder";
import { scaleValue } from "../renderer/scales";
import { createDefaultWorkspace } from "../state/createDefaultWorkspace";
import type { CalculationOutput } from "../types/calculations";
import type { Candle } from "../types/market";

function candles(count: number): Candle[] {
  return Array.from({ length: count }, (_, index) => ({
    symbol: "AAPL",
    timeframe: "1m",
    timestamp: `2026-01-01T00:${String(index).padStart(2, "0")}:00.000Z`,
    open: 100 + index,
    high: 101 + index,
    low: 99 + index,
    close: 100.5 + index,
    volume: 1000 + index * 20,
    finalized: true
  }));
}

describe("scene builder", () => {
  it("builds an empty scene for empty candles", () => {
    const chart = createDefaultWorkspace().charts[0];
    const scene = buildRenderScene({
      chart,
      candlesBySymbol: {},
      calculationOutputs: {},
      size: { width: 800, height: 480, devicePixelRatio: 1 }
    });

    expect(scene.visibleCandles).toHaveLength(0);
    expect(scene.panes).toHaveLength(2);
  });

  it("builds price and volume render layers for visible candles", () => {
    const chart = createDefaultWorkspace().charts[0];
    const scene = buildRenderScene({
      chart,
      candlesBySymbol: { AAPL: candles(220) },
      calculationOutputs: {},
      size: { width: 800, height: 480, devicePixelRatio: 1 }
    });

    const priceLayer = scene.layers.find((layer) => layer.type === "priceSeries");
    const volumeLayer = scene.layers.find((layer) => layer.type === "volume");
    expect(priceLayer?.type === "priceSeries" && priceLayer.candles.length).toBe(180);
    expect(volumeLayer?.type === "volume" && volumeLayer.candles.length).toBe(180);
    expect(scene.timeScale.ticks.length).toBeGreaterThan(1);
    expect(scene.panes[0].yScale.ticks).toHaveLength(4);
  });

  it("derives crosshair readout from the same scales used for rendering", () => {
    const chart = createDefaultWorkspace().charts[0];
    const scene = buildRenderScene({
      chart,
      candlesBySymbol: { AAPL: candles(220) },
      calculationOutputs: {},
      size: { width: 800, height: 480, devicePixelRatio: 1 }
    });
    const pricePane = scene.panes.find((pane) => pane.id === "pane-price");
    expect(pricePane).toBeTruthy();
    const x = pricePane!.xScale.timestampToX(scene.visibleCandles[20].timestamp)!;
    const y = pricePane!.yScale.valueToY(scene.visibleCandles[20].close);
    const withCrosshair = buildRenderScene({
      chart,
      candlesBySymbol: { AAPL: candles(220) },
      calculationOutputs: {},
      size: { width: 800, height: 480, devicePixelRatio: 1 },
      interaction: { crosshair: { x, y, paneId: "pane-price" } }
    });

    expect(withCrosshair.crosshairReadout?.timestamp).toBe(scene.visibleCandles[20].timestamp);
    expect(withCrosshair.crosshairReadout?.value).toBeCloseTo(scene.visibleCandles[20].close, 5);
  });

  it("keeps fixed logical ranges separate from the available data range", () => {
    const chart = {
      ...createDefaultWorkspace().charts[0],
      viewport: {
        ...createDefaultWorkspace().charts[0].viewport,
        mode: "fixedLogicalRange" as const,
        logicalFrom: 20.2,
        logicalTo: 29.7,
        visibleBars: 180,
        rightOffsetBars: 0
      }
    };

    const scene = buildRenderScene({
      chart,
      candlesBySymbol: { AAPL: candles(220) },
      calculationOutputs: {},
      size: { width: 800, height: 480, devicePixelRatio: 1 }
    });

    expect(scene.logicalRange).toEqual({ from: 20.2, to: 29.7 });
    expect(scene.visibleDataRange).toEqual({ from: 20, toExclusive: 31 });
    expect(scene.visibleCandles[0].timestamp).toBe("2026-01-01T00:20:00.000Z");
    expect(scene.visibleCandles[scene.visibleCandles.length - 1]?.timestamp).toBe("2026-01-01T00:30:00.000Z");
  });

  it("builds indicator and drawing render layers", () => {
    const workspace = createDefaultWorkspace();
    const chart = {
      ...workspace.charts[0],
      calculationGraph: {
        nodes: [{ id: "calc-sma", type: "SMA" as const, inputs: { source: "close", period: 3 }, outputKey: "sma" }]
      },
      layers: [
        ...workspace.charts[0].layers,
        {
          id: "layer-sma",
          type: "indicator" as const,
          owner: "ai" as const,
          paneId: "pane-price",
          zIndex: 200,
          visible: true,
          locked: false,
          style: { color: "#f59e0b" },
          calculationNodeId: "calc-sma",
          renderMode: "line" as const,
          createdAt: "2026-01-01T00:00:00.000Z",
          updatedAt: "2026-01-01T00:00:00.000Z"
        },
        {
          id: "layer-level",
          type: "drawing" as const,
          owner: "ai" as const,
          paneId: "pane-price",
          zIndex: 300,
          visible: true,
          locked: false,
          style: { color: "#38bdf8" },
          drawing: { kind: "horizontalLine" as const, price: 110, label: "Level" },
          createdAt: "2026-01-01T00:00:00.000Z",
          updatedAt: "2026-01-01T00:00:00.000Z"
        }
      ]
    };
    const output: CalculationOutput = {
      nodeId: "calc-sma",
      outputKey: "sma",
      series: [
        {
          key: "sma",
          label: "SMA",
          renderMode: "line",
          points: candles(30).map((candle) => ({ timestamp: candle.timestamp, value: candle.close }))
        }
      ],
      computedAt: "2026-01-01T00:00:00.000Z"
    };

    const scene = buildRenderScene({
      chart,
      candlesBySymbol: { AAPL: candles(30) },
      calculationOutputs: { "calc-sma": output },
      size: { width: 800, height: 480, devicePixelRatio: 1 }
    });

    expect(scene.layers.some((layer) => layer.type === "indicator")).toBe(true);
    const drawing = scene.layers.find((layer) => layer.type === "drawing");
    const pricePane = scene.panes.find((pane) => pane.id === "pane-price");
    expect(drawing?.type).toBe("drawing");
    expect(pricePane && scaleValue(pricePane.yScale, 110)).toBeGreaterThan(pricePane?.bounds.y ?? 0);
  });

  it("hit-tests horizontal line drawings through the renderer registry", () => {
    const workspace = createDefaultWorkspace();
    const chart = {
      ...workspace.charts[0],
      layers: [
        ...workspace.charts[0].layers,
        {
          id: "layer-level",
          type: "drawing" as const,
          owner: "user" as const,
          paneId: "pane-price",
          zIndex: 300,
          visible: true,
          locked: false,
          style: { color: "#38bdf8" },
          drawing: { kind: "horizontalLine" as const, price: 110, label: "Level" },
          createdAt: "2026-01-01T00:00:00.000Z",
          updatedAt: "2026-01-01T00:00:00.000Z"
        }
      ]
    };
    const scene = buildRenderScene({
      chart,
      candlesBySymbol: { AAPL: candles(40) },
      calculationOutputs: {},
      size: { width: 800, height: 480, devicePixelRatio: 1 }
    });
    const layer = scene.layers.find((item) => item.id === "layer-level");
    const pane = scene.panes.find((item) => item.id === "pane-price");
    expect(layer?.type).toBe("drawing");
    expect(pane).toBeTruthy();
    const y = pane!.yScale.valueToY(110);

    const hit = layer?.type === "drawing" ? defaultLayerRendererRegistry.drawing?.hitTest?.({ x: 200, y }, layer, pane!) : null;

    expect(hit?.layerId).toBe("layer-level");
    expect(hit?.drawingHandle).toBe("body");
  });

  it("does not include hidden layers in render data", () => {
    const workspace = createDefaultWorkspace();
    const chart = {
      ...workspace.charts[0],
      layers: [
        ...workspace.charts[0].layers,
        {
          id: "layer-hidden-level",
          type: "drawing" as const,
          owner: "user" as const,
          paneId: "pane-price",
          zIndex: 300,
          visible: false,
          locked: false,
          style: { color: "#38bdf8" },
          drawing: { kind: "horizontalLine" as const, price: 110, label: "Hidden" },
          createdAt: "2026-01-01T00:00:00.000Z",
          updatedAt: "2026-01-01T00:00:00.000Z"
        }
      ]
    };

    const scene = buildRenderScene({
      chart,
      candlesBySymbol: { AAPL: candles(40) },
      calculationOutputs: {},
      size: { width: 800, height: 480, devicePixelRatio: 1 }
    });

    expect(scene.layers.some((layer) => layer.id === "layer-hidden-level")).toBe(false);
  });

  it("keeps comparison percent values out of the price scale domain", () => {
    const workspace = createDefaultWorkspace();
    const chart = {
      ...workspace.charts[0],
      layers: [
        ...workspace.charts[0].layers,
        {
          id: "layer-msft",
          type: "comparisonSeries" as const,
          owner: "user" as const,
          paneId: "pane-price",
          zIndex: 180,
          visible: true,
          locked: false,
          style: { color: "#a78bfa" },
          symbol: "MSFT",
          baselineMode: "firstVisibleClose" as const,
          renderMode: "line" as const,
          createdAt: "2026-01-01T00:00:00.000Z",
          updatedAt: "2026-01-01T00:00:00.000Z"
        }
      ]
    };
    const comparisonCandles = candles(220).map((candle, index) => ({
      ...candle,
      symbol: "MSFT",
      open: 500 + index * 8,
      high: 506 + index * 8,
      low: 494 + index * 8,
      close: 502 + index * 8
    }));

    const scene = buildRenderScene({
      chart,
      candlesBySymbol: { AAPL: candles(220), MSFT: comparisonCandles },
      calculationOutputs: {},
      size: { width: 800, height: 480, devicePixelRatio: 1 }
    });

    const pricePane = scene.panes.find((pane) => pane.id === "pane-price");
    expect(scene.layers.some((layer) => layer.type === "comparisonSeries")).toBe(true);
    expect(pricePane?.yScale.domain[0]).toBeGreaterThan(80);
  });

  it("aligns comparison points to active candles and shares one percent scale", () => {
    const workspace = createDefaultWorkspace();
    const baseCandles = candles(30);
    const comparisonLayer = (id: string, symbol: string, color: string) => ({
      id,
      type: "comparisonSeries" as const,
      owner: "user" as const,
      paneId: "pane-price",
      zIndex: 180,
      visible: true,
      locked: false,
      style: { color },
      symbol,
      baselineMode: "firstVisibleClose" as const,
      renderMode: "line" as const,
      createdAt: "2026-01-01T00:00:00.000Z",
      updatedAt: "2026-01-01T00:00:00.000Z"
    });
    const chart = {
      ...workspace.charts[0],
      layers: [
        ...workspace.charts[0].layers,
        comparisonLayer("layer-msft", "MSFT", "#a78bfa"),
        comparisonLayer("layer-nvda", "NVDA", "#38bdf8")
      ]
    };
    const msftCandles = baseCandles
      .filter((_, index) => index % 2 === 0)
      .map((candle, index) => ({ ...candle, symbol: "MSFT", close: 200 + index }));
    const nvdaCandles = baseCandles.map((candle, index) => ({ ...candle, symbol: "NVDA", close: 500 + index * 10 }));

    const scene = buildRenderScene({
      chart,
      candlesBySymbol: { AAPL: baseCandles, MSFT: msftCandles, NVDA: nvdaCandles },
      calculationOutputs: {},
      size: { width: 800, height: 480, devicePixelRatio: 1 }
    });

    const pricePane = scene.panes.find((pane) => pane.id === "pane-price");
    const msft = scene.layers.find((layer) => layer.id === "layer-msft");
    expect(msft?.type === "comparisonSeries" && msft.points).toHaveLength(scene.visibleCandles.length);
    expect(msft?.type === "comparisonSeries" && msft.points[1].value).toBeNull();
    expect(msft?.type === "comparisonSeries" && msft.baseTimestamp).toBe(scene.visibleCandles[0].timestamp);
    expect(pricePane?.comparisonScale?.domain[1]).toBeGreaterThan(40);
    expect(pricePane?.comparisonScale?.domain[0]).toBeLessThanOrEqual(0);
    expect(pricePane?.comparisonBases[0]?.symbol).toBe("MSFT");
  });

  it("keeps comparison scale stable when only the live last candle updates", () => {
    const workspace = createDefaultWorkspace();
    const baseCandles = candles(40);
    const chart = {
      ...workspace.charts[0],
      layers: [
        ...workspace.charts[0].layers,
        {
          id: "layer-msft",
          type: "comparisonSeries" as const,
          owner: "user" as const,
          paneId: "pane-price",
          zIndex: 180,
          visible: true,
          locked: false,
          style: { color: "#a78bfa" },
          symbol: "MSFT",
          baselineMode: "firstVisibleCompleteBar" as const,
          normalization: "percentFromFirstVisibleCompleteBar" as const,
          renderMode: "line" as const,
          createdAt: "2026-01-01T00:00:00.000Z",
          updatedAt: "2026-01-01T00:00:00.000Z"
        }
      ]
    };
    const comparisonCandles = baseCandles.map((candle, index) => ({
      ...candle,
      symbol: "MSFT",
      close: 200 + index
    }));
    const initialScene = buildRenderScene({
      chart,
      candlesBySymbol: { AAPL: baseCandles, MSFT: comparisonCandles },
      calculationOutputs: {},
      size: { width: 800, height: 480, devicePixelRatio: 1 }
    });
    const updatedComparisonCandles = comparisonCandles.map((candle, index) =>
      index === comparisonCandles.length - 1 ? { ...candle, close: candle.close * 1.25 } : candle
    );
    const updatedScene = buildRenderScene({
      chart,
      candlesBySymbol: { AAPL: baseCandles, MSFT: updatedComparisonCandles },
      calculationOutputs: {},
      size: { width: 800, height: 480, devicePixelRatio: 1 }
    });

    const initialDomain = initialScene.panes.find((pane) => pane.id === "pane-price")?.comparisonScale?.domain;
    const updatedDomain = updatedScene.panes.find((pane) => pane.id === "pane-price")?.comparisonScale?.domain;
    const initialLayer = initialScene.layers.find((layer) => layer.id === "layer-msft");
    const updatedLayer = updatedScene.layers.find((layer) => layer.id === "layer-msft");
    expect(updatedDomain).toEqual(initialDomain);
    expect(
      initialLayer?.type === "comparisonSeries" &&
        updatedLayer?.type === "comparisonSeries" &&
        updatedLayer.points.slice(0, -1).map((point) => point.value)
    ).toEqual(
      initialLayer?.type === "comparisonSeries" ? initialLayer.points.slice(0, -1).map((point) => point.value) : []
    );
    expect(updatedLayer?.type === "comparisonSeries" && updatedLayer.points[updatedLayer.points.length - 1]?.value).toBeGreaterThan(20);
  });

  it("crosshair scene construction does not mutate documents", () => {
    const chart = createDefaultWorkspace().charts[0];
    const before = structuredClone(chart);
    buildRenderScene({
      chart,
      candlesBySymbol: { AAPL: candles(40) },
      calculationOutputs: {},
      size: { width: 800, height: 480, devicePixelRatio: 1 },
      interaction: { crosshair: { x: 100, y: 120, paneId: "pane-price" } }
    });

    expect(chart).toEqual(before);
  });
});
