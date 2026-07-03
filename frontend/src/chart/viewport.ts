const MIN_VISIBLE_CANDLES = 12;
const MAX_VISIBLE_CANDLES = 500;
const MIN_READABLE_SLOT_WIDTH = 4;
const FUTURE_EMPTY_SPACE_RATIO = 2 / 3;
const INITIAL_RIGHT_EMPTY_SPACE_RATIO = 1 / 3;

export type ChartViewport = {
  visibleCount: number;
  rightOffset: number;
};

export function clampVisibleCount(visibleCount: number, candleCount: number, plotWidth?: number): number {
  const widthBound = typeof plotWidth === "number"
    ? Math.max(MIN_VISIBLE_CANDLES, Math.floor(Math.max(1, plotWidth) / MIN_READABLE_SLOT_WIDTH))
    : MAX_VISIBLE_CANDLES;
  const dataBound = candleCount > 0 ? Math.max(MIN_VISIBLE_CANDLES, candleCount) : MAX_VISIBLE_CANDLES;
  const maxVisibleCount = Math.max(MIN_VISIBLE_CANDLES, Math.min(MAX_VISIBLE_CANDLES, widthBound, dataBound));
  return Math.max(MIN_VISIBLE_CANDLES, Math.min(maxVisibleCount, Math.round(visibleCount)));
}

export function clampRightOffset(rightOffset: number, visibleCount: number, candleCount: number): number {
  const safeCandleCount = Math.max(0, Math.floor(candleCount));
  const safeVisibleCount = Math.max(1, Math.round(visibleCount));
  const maxRightOffset = Math.max(0, safeCandleCount - Math.min(safeVisibleCount, safeCandleCount));
  const minRightOffset = -futureEmptySlotCount(safeVisibleCount);
  const rounded = Number.isFinite(rightOffset) ? Math.round(rightOffset) : 0;
  return Math.max(minRightOffset, Math.min(maxRightOffset, rounded));
}

export function normalizeViewport(viewport: ChartViewport, candleCount: number, plotWidth?: number): ChartViewport {
  const visibleCount = clampVisibleCount(viewport.visibleCount, candleCount, plotWidth);
  return {
    visibleCount,
    rightOffset: clampRightOffset(viewport.rightOffset, visibleCount, candleCount)
  };
}

export function zoomViewport(viewport: ChartViewport, visibleCountDelta: number, candleCount: number, plotWidth?: number): ChartViewport {
  return normalizeViewport(
    {
      visibleCount: viewport.visibleCount + visibleCountDelta,
      rightOffset: viewport.rightOffset
    },
    candleCount,
    plotWidth
  );
}

export function zoomViewportAt(
  viewport: ChartViewport,
  visibleCountDelta: number,
  candleCount: number,
  anchorRatio: number,
  plotWidth?: number
): ChartViewport {
  const current = normalizeViewport(viewport, candleCount, plotWidth);
  const nextVisibleCount = clampVisibleCount(current.visibleCount + visibleCountDelta, candleCount, plotWidth);
  const ratio = Math.max(0, Math.min(1, Number.isFinite(anchorRatio) ? anchorRatio : 0.5));
  const currentEndIndex = candleCount - current.rightOffset;
  const currentStartIndex = currentEndIndex - current.visibleCount;
  const anchorIndex = currentStartIndex + current.visibleCount * ratio;
  const nextStartIndex = anchorIndex - nextVisibleCount * ratio;
  const nextRightOffset = candleCount - (nextStartIndex + nextVisibleCount);
  return normalizeViewport(
    {
      visibleCount: nextVisibleCount,
      rightOffset: nextRightOffset
    },
    candleCount,
    plotWidth
  );
}

export function dragDeltaToRightOffset(
  startRightOffset: number,
  dragPixels: number,
  slotWidth: number,
  visibleCount: number,
  candleCount: number
): number {
  const slotDelta = Math.round(dragPixels / Math.max(0.0001, slotWidth));
  return clampRightOffset(startRightOffset + slotDelta, visibleCount, candleCount);
}

export function futureEmptySlotCount(visibleCount: number): number {
  return Math.max(0, Math.ceil(Math.max(1, Math.round(visibleCount)) * FUTURE_EMPTY_SPACE_RATIO));
}

export function latestCandleRightOffset(visibleCount: number): number {
  return -Math.max(0, Math.ceil(Math.max(1, Math.round(visibleCount)) * INITIAL_RIGHT_EMPTY_SPACE_RATIO));
}
