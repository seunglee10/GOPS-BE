import { createPercentScale, createTimeScale } from "./scales";
import type { CandleData, ChartCrosshair, ChartDocument, ChartLoadState, ChartPendingPreview, RenderScene, StreamStatus } from "./types";

export function buildRenderScene({
  state,
  message,
  document,
  candles,
  width,
  height,
  crosshair,
  streamStatus,
  comparisonCandlesBySymbol = {},
  pendingPreview
}: {
  state: ChartLoadState;
  message?: string;
  document: ChartDocument;
  candles: CandleData[];
  width: number;
  height: number;
  crosshair?: { x: number; y: number };
  streamStatus?: StreamStatus;
  comparisonCandlesBySymbol?: Record<string, CandleData[]>;
  pendingPreview?: ChartPendingPreview;
}): RenderScene {
  const safeWidth = Math.max(1, width);
  const safeHeight = Math.max(1, height);
  const variant = resolveChartSizeVariant(safeWidth, safeHeight);
  const left = variant === "compact" ? 14 : 18;
  const right = safeWidth - (variant === "compact" ? 54 : 64);
  const top = variant === "compact" ? 12 : 14;
  const bottom = safeHeight - (variant === "compact" ? 20 : 24);
  const volumeHeight = variant === "compact" ? Math.max(34, safeHeight * 0.18) : Math.max(46, safeHeight * 0.22);
  const priceBottom = Math.max(top + 40, bottom - volumeHeight - 12);
  const volumeTop = priceBottom + 12;
  const plotWidth = Math.max(1, right - left);
  const visibleCount = resolveVisibleCount(plotWidth, document.viewport.visibleCount);
  const rightOffset = Math.min(Math.max(0, document.viewport.rightOffset), Math.max(0, candles.length - 1));
  const visibleEnd = Math.max(0, candles.length - rightOffset);
  const visibleStart = Math.max(0, visibleEnd - visibleCount);
  const visibleCandles = candles.slice(visibleStart, visibleEnd);
  const timeScale = createTimeScale({
    candles,
    visibleCandles,
    visibleStartIndex: visibleStart,
    visibleEndIndex: visibleEnd,
    left,
    right
  });
  const prices = visibleCandles.flatMap((candle) => [
    candle.high,
    candle.low,
    candle.ma5,
    candle.ma20,
    candle.ma60
  ]).filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const minPrice = prices.length ? Math.min(...prices) : 0;
  const maxPrice = prices.length ? Math.max(...prices) : 1;
  const maxVolume = Math.max(1, ...visibleCandles.map((candle) => candle.volume));
  const comparisonSeries = buildComparisonSeries({
    document,
    comparisonCandlesBySymbol,
    visibleCandles,
    timeScale,
    top,
    priceBottom
  });
  const comparisonPercents = comparisonSeries.flatMap((series) => series.points.map((point) => point.percent));
  const minPercent = comparisonPercents.length ? Math.min(-1, ...comparisonPercents) : -1;
  const maxPercent = comparisonPercents.length ? Math.max(1, ...comparisonPercents) : 1;
  const percentScale = createPercentScale(minPercent, maxPercent, top, priceBottom);
  const scaledComparisonSeries = comparisonSeries.map((series) => ({
    ...series,
    points: series.points.map((point) => ({ ...point, y: percentScale.percentToY(point.percent) }))
  }));
  const slotWidth = plotWidth / Math.max(1, visibleCandles.length);
  const candleWidth = Math.max(2, Math.min(13, slotWidth * 0.62));
  const last = visibleCandles[visibleCandles.length - 1];
  const first = visibleCandles[0];
  const change = first && last ? ((last.close - first.open) / Math.max(0.0001, first.open)) * 100 : undefined;

  const sceneBase = {
    state,
    message,
    width: safeWidth,
    height: safeHeight,
    document,
    candles: visibleCandles,
    allCandles: candles,
    visibleStartIndex: visibleStart,
    visibleEndIndex: visibleEnd,
    pendingPreview,
    variant,
    plot: {
      left,
      top,
      right,
      bottom,
      priceBottom,
      volumeTop
    },
    scales: {
      minPrice,
      maxPrice,
      maxVolume,
      minPercent,
      maxPercent,
      candleWidth,
      gap: Math.max(1, slotWidth - candleWidth)
    },
    comparisonSeries: scaledComparisonSeries,
    labels: {
      symbol: document.symbol,
      timeframe: document.timeframe,
      lastPrice: last ? last.close.toFixed(2) : undefined,
      range: prices.length ? `${minPrice.toFixed(2)} - ${maxPrice.toFixed(2)}` : undefined,
      change: typeof change === "number" ? `${change >= 0 ? "+" : ""}${change.toFixed(2)}%` : undefined,
      visibleHigh: prices.length ? maxPrice.toFixed(2) : undefined,
      visibleLow: prices.length ? minPrice.toFixed(2) : undefined,
      streamStatus
    }
  } satisfies RenderScene;

  return {
    ...sceneBase,
    crosshair: crosshair ? resolveCrosshair(sceneBase, crosshair.x, crosshair.y) : undefined
  };
}

function buildComparisonSeries({
  document,
  comparisonCandlesBySymbol,
  visibleCandles,
  timeScale,
  top,
  priceBottom
}: {
  document: ChartDocument;
  comparisonCandlesBySymbol: Record<string, CandleData[]>;
  visibleCandles: CandleData[];
  timeScale: ReturnType<typeof createTimeScale>;
  top: number;
  priceBottom: number;
}): RenderScene["comparisonSeries"] {
  void top;
  void priceBottom;
  return document.comparisons.map((comparison) => {
    const candles = comparisonCandlesBySymbol[comparison.symbol] ?? [];
    const visibleTimestampSet = new Set(visibleCandles.map((candle) => candle.timestamp));
    const aligned = candles.filter((candle) => visibleTimestampSet.has(candle.timestamp));
    const baseCandle = comparison.base?.mode === "timestamp" && comparison.base.timestamp
      ? candles.find((candle) => candle.timestamp === comparison.base?.timestamp)
      : aligned[0];
    const baseClose = baseCandle?.close ?? aligned[0]?.close ?? candles[0]?.close;
    const points = typeof baseClose === "number" && baseClose !== 0
      ? aligned.flatMap((candle) => {
        const x = timeScale.timestampToX(candle.timestamp);
        if (x === null) {
          return [];
        }
        const percent = ((candle.close - baseClose) / Math.max(0.0001, baseClose)) * 100;
        return [{ x, y: 0, percent, candle }];
      })
      : [];

    return { comparison, candles: aligned, points };
  });
}

function resolveVisibleCount(plotWidth: number, requestedVisibleCount: number): number {
  const minimumReadableSlotWidth = 8;
  const widthBoundCount = Math.max(12, Math.floor(plotWidth / minimumReadableSlotWidth));
  return Math.max(1, Math.min(requestedVisibleCount, widthBoundCount));
}

export function resolveChartSizeVariant(width: number, height: number): RenderScene["variant"] {
  if (width < 280 || height < 190) {
    return "compact";
  }
  if (width >= 640 && height >= 320) {
    return "large";
  }
  if (width >= 470) {
    return "wide";
  }
  return "standard";
}

export function resolveCrosshair(scene: RenderScene, x: number, y: number): ChartCrosshair | undefined {
  if (scene.state !== "ready" || scene.candles.length === 0) {
    return undefined;
  }

  if (x < scene.plot.left || x > scene.plot.right || y < scene.plot.top || y > scene.plot.bottom) {
    return undefined;
  }

  const slot = (scene.plot.right - scene.plot.left) / Math.max(1, scene.candles.length);
  const candleIndex = Math.max(0, Math.min(scene.candles.length - 1, Math.floor((x - scene.plot.left) / slot)));
  const candle = scene.candles[candleIndex];
  if (!candle) {
    return undefined;
  }

  return {
    x: scene.plot.left + slot * candleIndex + slot / 2,
    y,
    candleIndex,
    candle,
    price: priceFromY(scene, y)
  };
}

export function priceFromY(scene: RenderScene, y: number): number {
  const range = Math.max(0.0001, scene.scales.maxPrice - scene.scales.minPrice);
  const ratio = (y - scene.plot.top) / Math.max(1, scene.plot.priceBottom - scene.plot.top);
  return scene.scales.maxPrice - range * ratio;
}
