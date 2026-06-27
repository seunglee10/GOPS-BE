export const MIN_VISIBLE_CANDLES = 12;
export const MAX_VISIBLE_CANDLES = 180;
export const MIN_READABLE_SLOT_WIDTH = 8;

export function resolveViewportVisibleCount(plotWidth: number, requestedVisibleCount: number): number {
  const widthBoundCount = Math.max(MIN_VISIBLE_CANDLES, Math.floor(plotWidth / MIN_READABLE_SLOT_WIDTH));
  return Math.max(1, Math.min(Math.round(requestedVisibleCount), widthBoundCount));
}

export function clampVisibleCount(
  visibleCount: number,
  candleCount: number,
  plotWidth?: number
): number {
  const widthBound = typeof plotWidth === "number"
    ? Math.max(MIN_VISIBLE_CANDLES, Math.floor(Math.max(1, plotWidth) / MIN_READABLE_SLOT_WIDTH))
    : MAX_VISIBLE_CANDLES;
  const dataBound = candleCount > 0 ? Math.max(MIN_VISIBLE_CANDLES, candleCount) : MAX_VISIBLE_CANDLES;
  const maxVisibleCount = Math.max(
    MIN_VISIBLE_CANDLES,
    Math.min(MAX_VISIBLE_CANDLES, widthBound, dataBound)
  );
  return Math.max(MIN_VISIBLE_CANDLES, Math.min(maxVisibleCount, Math.round(visibleCount)));
}

export function normalizeViewport(
  viewport: { visibleCount: number; rightOffset: number },
  candleCount: number,
  plotWidth?: number
): { visibleCount: number; rightOffset: number } {
  const visibleCount = clampVisibleCount(viewport.visibleCount, candleCount, plotWidth);
  return {
    visibleCount,
    rightOffset: clampRightOffset(viewport.rightOffset, visibleCount, candleCount)
  };
}

export function zoomViewport(
  viewport: { visibleCount: number; rightOffset: number },
  visibleCountDelta: number,
  candleCount: number,
  plotWidth?: number
): { visibleCount: number; rightOffset: number } {
  const baseVisibleCount = clampVisibleCount(viewport.visibleCount, candleCount, plotWidth);
  return normalizeViewport(
    {
      visibleCount: baseVisibleCount + visibleCountDelta,
      rightOffset: viewport.rightOffset
    },
    candleCount,
    plotWidth
  );
}

export function clampRightOffset(rightOffset: number, visibleCount: number, candleCount: number): number {
  const maxRightOffset = Math.max(0, candleCount - Math.max(1, Math.min(visibleCount, candleCount)));
  return Math.max(0, Math.min(maxRightOffset, Math.round(rightOffset)));
}

export function dragDeltaToRightOffset(
  startRightOffset: number,
  dragPixels: number,
  slotWidth: number,
  visibleCount: number,
  candleCount: number
): number {
  const slotDelta = Math.round(dragPixels / Math.max(1, slotWidth));
  return clampRightOffset(startRightOffset + slotDelta, visibleCount, candleCount);
}
