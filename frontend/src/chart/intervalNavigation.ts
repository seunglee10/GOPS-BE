import type { CandleDto, ChartInterval } from "./types";
import { chartIntervals, defaultVisibleBarsForInterval } from "./types";
import { latestCandleRightOffset, normalizeViewport, type ChartViewport } from "./viewport";

export type IntervalDirection = "smaller" | "larger";

export type ViewportAnchor = {
  mode: "right" | "center";
  timestamp?: string;
  visibleCount?: number;
};

export function adjacentInterval(interval: ChartInterval, direction: IntervalDirection): ChartInterval | null {
  const index = chartIntervals.indexOf(interval);
  if (index < 0) {
    return null;
  }
  const nextIndex = direction === "smaller" ? index - 1 : index + 1;
  return chartIntervals[nextIndex] ?? null;
}

export function anchoredViewportForCandles(
  candles: CandleDto[],
  interval: ChartInterval,
  anchor?: ViewportAnchor | null,
  fallback?: ChartViewport,
  plotWidth?: number
): ChartViewport {
  const preferredVisibleCount = anchor?.visibleCount ?? fallback?.visibleCount ?? defaultVisibleBarsForInterval(interval);
  const fallbackUsesLatestSpace = !fallback || fallback.rightOffset === latestCandleRightOffset(fallback.visibleCount);
  if (!anchor?.timestamp || candles.length === 0) {
    const visibleViewport = normalizeViewport(
      {
        visibleCount: preferredVisibleCount,
        rightOffset: fallback?.rightOffset ?? 0
      },
      candles.length,
      plotWidth
    );
    return fallbackUsesLatestSpace
      ? normalizeViewport(
          {
            visibleCount: visibleViewport.visibleCount,
            rightOffset: latestCandleRightOffset(visibleViewport.visibleCount)
          },
          candles.length,
          plotWidth
        )
      : visibleViewport;
  }

  const anchorIndex = findCandleIndexAtOrBefore(candles, anchor.timestamp);
  if (anchorIndex < 0) {
    const visibleViewport = normalizeViewport(
      {
        visibleCount: preferredVisibleCount,
        rightOffset: fallback?.rightOffset ?? 0
      },
      candles.length,
      plotWidth
    );
    return fallbackUsesLatestSpace
      ? normalizeViewport(
          {
            visibleCount: visibleViewport.visibleCount,
            rightOffset: latestCandleRightOffset(visibleViewport.visibleCount)
          },
          candles.length,
          plotWidth
        )
      : visibleViewport;
  }

  const visibleCount = normalizeViewport(
    {
      visibleCount: preferredVisibleCount,
      rightOffset: fallback?.rightOffset ?? 0
    },
    candles.length,
    plotWidth
  ).visibleCount;
  const viewportEndIndex = anchor.mode === "center"
    ? anchorIndex + 1 + Math.floor(visibleCount / 2)
    : anchorIndex + 1;

  return normalizeViewport(
    {
      visibleCount,
      rightOffset: candles.length - viewportEndIndex
    },
    candles.length,
    plotWidth
  );
}

function findCandleIndexAtOrBefore(candles: CandleDto[], timestamp: string): number {
  const target = new Date(timestamp).getTime();
  if (!Number.isFinite(target)) {
    return -1;
  }
  let best = -1;
  for (let index = 0; index < candles.length; index += 1) {
    const value = new Date(candles[index].timestamp).getTime();
    if (!Number.isFinite(value)) {
      continue;
    }
    if (value <= target) {
      best = index;
      continue;
    }
    break;
  }
  return best;
}
