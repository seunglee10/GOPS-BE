import { createPercentScale, createTimeScale } from "./scales";
import type { CandleData, ChartCrosshair, ChartDocument, ChartLoadState, ChartPendingPreview, RenderScene, StreamStatus } from "./types";
import { clampRightOffset, resolveViewportVisibleCount } from "./viewport";

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
  const visibleCount = resolveViewportVisibleCount(plotWidth, document.viewport.visibleCount);
  const rightOffset = clampRightOffset(document.viewport.rightOffset, visibleCount, candles.length);
  const viewportEndIndex = Math.max(0, candles.length - rightOffset);
  const viewportStartIndex = viewportEndIndex - visibleCount;
  const visibleStart = Math.max(0, Math.min(candles.length, Math.floor(viewportStartIndex)));
  const visibleEnd = Math.max(visibleStart, Math.min(candles.length, Math.ceil(viewportEndIndex)));
  const futureSlotCount = Math.max(0, viewportEndIndex - candles.length);
  const visibleCandles = candles.slice(visibleStart, visibleEnd);
  const timeScale = createTimeScale({
    candles,
    visibleCandles,
    visibleStartIndex: visibleStart,
    visibleEndIndex: visibleEnd,
    viewportStartIndex,
    viewportEndIndex,
    visibleSlotCount: visibleCount,
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
  const rawMinPrice = prices.length ? Math.min(...prices) : 0;
  const rawMaxPrice = prices.length ? Math.max(...prices) : 1;
  const { minPrice, maxPrice } = prices.length
    ? paddedPriceRange(rawMinPrice, rawMaxPrice)
    : { minPrice: rawMinPrice, maxPrice: rawMaxPrice };
  const maxVolume = Math.max(1, ...visibleCandles.map((candle) => candle.volume));
  const comparisonSeries = buildComparisonSeries({
    document,
    pendingPreview,
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
  const slotWidth = plotWidth / Math.max(1, visibleCount);
  const candleWidth = slotWidth < 2
    ? Math.max(0.2, slotWidth * 0.8)
    : Math.max(2, Math.min(13, slotWidth * 0.62));
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
    viewportStartIndex,
    viewportEndIndex,
    visibleSlotCount: visibleCount,
    futureSlotCount,
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
      slotWidth,
      candleWidth,
      gap: Math.max(0, slotWidth - candleWidth)
    },
    comparisonSeries: scaledComparisonSeries,
    labels: {
      symbol: document.symbol,
      timeframe: document.timeframe,
      lastPrice: last ? last.close.toFixed(2) : undefined,
      range: prices.length ? `${rawMinPrice.toFixed(2)} - ${rawMaxPrice.toFixed(2)}` : undefined,
      change: typeof change === "number" ? `${change >= 0 ? "+" : ""}${change.toFixed(2)}%` : undefined,
      visibleHigh: prices.length ? rawMaxPrice.toFixed(2) : undefined,
      visibleLow: prices.length ? rawMinPrice.toFixed(2) : undefined,
      streamStatus
    }
  } satisfies RenderScene;

  return {
    ...sceneBase,
    crosshair: crosshair ? resolveCrosshair(sceneBase, crosshair.x, crosshair.y) : undefined
  };
}

function paddedPriceRange(minPrice: number, maxPrice: number): { minPrice: number; maxPrice: number } {
  const range = Math.max(0, maxPrice - minPrice);
  const fallbackPadding = Math.max(Math.abs(maxPrice), Math.abs(minPrice), 1) * 0.002;
  const padding = Math.max(range * 0.06, fallbackPadding);
  return {
    minPrice: minPrice - padding,
    maxPrice: maxPrice + padding
  };
}

function buildComparisonSeries({
  document,
  pendingPreview,
  comparisonCandlesBySymbol,
  visibleCandles,
  timeScale,
  top,
  priceBottom
}: {
  document: ChartDocument;
  pendingPreview?: ChartPendingPreview;
  comparisonCandlesBySymbol: Record<string, CandleData[]>;
  visibleCandles: CandleData[];
  timeScale: ReturnType<typeof createTimeScale>;
  top: number;
  priceBottom: number;
}): RenderScene["comparisonSeries"] {
  void top;
  void priceBottom;
  const previewComparisons = pendingPreview?.visible
    ? pendingPreview.comparisons
      .filter((comparison) => !document.comparisons.some((existing) => existing.symbol === comparison.symbol))
      .map((comparison) => ({
        ...comparison,
        label: comparison.label ?? `${comparison.symbol} preview`,
        style: {
          ...(comparison.style ?? {}),
          lineDash: comparison.style?.lineDash ?? [6, 4],
          opacity: comparison.style?.opacity ?? 0.62
        }
      }))
    : [];
  return [...document.comparisons, ...previewComparisons].map((comparison) => {
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
  void plotWidth;
  return Math.max(1, Math.floor(requestedVisibleCount));
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

  const slot = scene.scales.slotWidth;
  const logicalIndex = Math.round(scene.viewportStartIndex + (x - scene.plot.left - slot / 2) / Math.max(0.0001, slot));
  if (logicalIndex < scene.visibleStartIndex || logicalIndex >= scene.visibleEndIndex) {
    return undefined;
  }

  const candle = scene.allCandles[logicalIndex];
  if (!candle) {
    return undefined;
  }

  return {
    x: scene.plot.left + slot * (logicalIndex - scene.viewportStartIndex) + slot / 2,
    y,
    candleIndex: logicalIndex - scene.visibleStartIndex,
    candle,
    price: priceFromY(scene, y)
  };
}

export function priceFromY(scene: RenderScene, y: number): number {
  const range = Math.max(0.0001, scene.scales.maxPrice - scene.scales.minPrice);
  const ratio = (y - scene.plot.top) / Math.max(1, scene.plot.priceBottom - scene.plot.top);
  return scene.scales.maxPrice - range * ratio;
}
