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
