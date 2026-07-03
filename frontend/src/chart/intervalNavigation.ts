import type { CandleDto, ChartInterval } from "./types";
import { chartIntervals, defaultVisibleBarsForInterval } from "./types";
import { latestCandleRightOffset, normalizeViewport, type ChartViewport } from "./viewport";

export type IntervalDirection = "smaller" | "larger";

export type ViewportAnchor = {
  mode: "right" | "center";
  timestamp?: string;
  visibleCount?: number;
};

export type IntervalQueryRange = {
  from: string;
  to: string;
  limit: number;
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

export function intervalQueryRangeAround(timestamp: string, interval: ChartInterval, visibleCount: number): IntervalQueryRange | null {
  const anchor = new Date(timestamp);
  if (!Number.isFinite(anchor.getTime())) {
    return null;
  }
  const count = Math.max(1, Math.round(visibleCount));
  const before = Math.floor(count / 2);
  const after = Math.max(1, count - before);
  const from = stepInterval(anchor, interval, -before);
  const to = stepInterval(anchor, interval, after);
  return {
    from: toIso(from),
    to: toIso(to),
    limit: count
  };
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

function stepInterval(date: Date, interval: ChartInterval, steps: number): Date {
  const next = new Date(date.getTime());
  switch (interval) {
    case "1m":
      next.setUTCMinutes(next.getUTCMinutes() + steps);
      return next;
    case "5m":
      next.setUTCMinutes(next.getUTCMinutes() + steps * 5);
      return next;
    case "10m":
      next.setUTCMinutes(next.getUTCMinutes() + steps * 10);
      return next;
    case "1D":
      next.setUTCDate(next.getUTCDate() + steps);
      return next;
    case "1W":
      next.setUTCDate(next.getUTCDate() + steps * 7);
      return next;
    case "1M":
      return new Date(Date.UTC(next.getUTCFullYear(), next.getUTCMonth() + steps, 1));
  }
}

function toIso(date: Date): string {
  return date.toISOString().replace(".000Z", "Z");
}
