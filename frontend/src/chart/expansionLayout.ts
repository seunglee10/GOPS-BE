export type HorizontalBounds = {
  left: number;
  right: number;
};

export const expansionParentCandleHeight = 27;
export const expansionParentCandleWidth = 6;
export const expansionMetadataGridGap = 2;
export const expansionCloseButtonSize = 24;

export function expansionMetadataTop(plotTop: number): number {
  return Math.max(24, plotTop - expansionMetadataGridGap - expansionParentCandleHeight);
}

export function expansionMetadataCenterY(plotTop: number): number {
  return expansionMetadataTop(plotTop) + expansionParentCandleHeight / 2;
}

export function expansionSummaryVisibleBounds(plot: HorizontalBounds, range: HorizontalBounds): HorizontalBounds {
  return {
    left: Math.max(plot.left + 8, range.left + 8),
    right: Math.min(plot.right - 8, range.right - 8)
  };
}

export function expansionParentThumbnailRight(plot: HorizontalBounds, range: HorizontalBounds): number {
  const bounds = expansionSummaryVisibleBounds(plot, range);
  const availableWidth = bounds.right - bounds.left;
  if (availableWidth < 16) {
    return bounds.left;
  }
  const candleCenter = Math.round(Math.min(bounds.left + 9, bounds.left + availableWidth / 2)) + 0.5;
  return candleCenter + expansionParentCandleWidth / 2;
}
