export const chartIntervals = ["1m", "5m", "10m", "1D", "1W", "1M"] as const;

export type ChartInterval = typeof chartIntervals[number];

export const rangeBackfillBufferMultiplier = 2;

const rangeBackfillBufferMultipliers: Record<ChartInterval, number> = {
  "1m": 3,
  "5m": 3,
  "10m": 2.5,
  "1D": 2.5,
  "1W": rangeBackfillBufferMultiplier,
  "1M": rangeBackfillBufferMultiplier
};

const defaultVisibleBars: Record<ChartInterval, number> = {
  "1m": 390,
  "5m": 390,
  "10m": 390,
  "1D": 250,
  "1W": 260,
  "1M": 120
};

const requestPageBars: Record<ChartInterval, number> = {
  "1m": 5000,
  "5m": 3000,
  "10m": 3000,
  "1D": 3000,
  "1W": 1000,
  "1M": 1000
};

const minimumBackfillSourceBars: Record<ChartInterval, number> = {
  "1m": 390,
  "5m": 390,
  "10m": 390,
  "1D": 250,
  "1W": 260,
  "1M": 252
};

export function normalizeChartInterval(value: unknown): ChartInterval | null {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  if (trimmed === "1d") {
    return "1D";
  }
  if (trimmed === "1w") {
    return "1W";
  }
  if (trimmed === "1mo" || trimmed === "1MO" || trimmed === "1month") {
    return "1M";
  }
  return chartIntervals.includes(trimmed as ChartInterval) ? trimmed as ChartInterval : null;
}

export function defaultVisibleBarsForInterval(interval: string): number {
  return defaultVisibleBars[normalizeChartInterval(interval) ?? "1m"];
}

export function maxRequestBarsForInterval(interval: string): number {
  return requestPageBars[normalizeChartInterval(interval) ?? "1m"];
}

export function minimumBackfillSourceBarsForInterval(interval: string): number {
  return minimumBackfillSourceBars[normalizeChartInterval(interval) ?? "1m"];
}

export function rangeBackfillBufferMultiplierForInterval(interval: string): number {
  return rangeBackfillBufferMultipliers[normalizeChartInterval(interval) ?? "1m"];
}
