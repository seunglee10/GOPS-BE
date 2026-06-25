import type { RenderBounds } from "./sceneBuilder";
import type { Candle } from "../types/market";

export interface TimeAxisTick {
  logical: number;
  x: number;
  timestamp: string;
  label: string;
}

export interface TimeScaleModel {
  logicalRange: { from: number; to: number };
  visibleDataRange: { from: number; toExclusive: number };
  bounds: Pick<RenderBounds, "x" | "width">;
  barSpacing: number;
  candleBodyWidth: number;
  ticks: TimeAxisTick[];
  logicalToX(logical: number): number;
  xToLogical(x: number): number;
  timestampToX(timestamp: string): number | null;
  timestampToLogical(timestamp: string): number | null;
}

export interface CreateTimeScaleModelArgs {
  bounds: Pick<RenderBounds, "x" | "width">;
  logicalRange: { from: number; to: number };
  visibleDataRange: { from: number; toExclusive: number };
  visibleCandles: Candle[];
}

export function createTimeScaleModel({
  bounds,
  logicalRange,
  visibleDataRange,
  visibleCandles
}: CreateTimeScaleModelArgs): TimeScaleModel {
  const span = Math.max(1, logicalRange.to - logicalRange.from + 1);
  const barSpacing = bounds.width / span;
  const timestampToLogicalMap = new Map<string, number>();
  visibleCandles.forEach((candle, visibleIndex) => {
    timestampToLogicalMap.set(candle.timestamp, visibleDataRange.from + visibleIndex);
  });

  const model: TimeScaleModel = {
    logicalRange,
    visibleDataRange,
    bounds,
    barSpacing,
    candleBodyWidth: Math.max(2, Math.min(14, barSpacing * 0.58)),
    ticks: [],
    logicalToX(logical) {
      return bounds.x + (logical - logicalRange.from + 0.5) * barSpacing;
    },
    xToLogical(x) {
      return (x - bounds.x) / barSpacing + logicalRange.from - 0.5;
    },
    timestampToX(timestamp) {
      const logical = timestampToLogicalMap.get(timestamp);
      return typeof logical === "number" ? model.logicalToX(logical) : null;
    },
    timestampToLogical(timestamp) {
      return timestampToLogicalMap.get(timestamp) ?? null;
    }
  };
  model.ticks = buildTimeTicks(model, visibleCandles, visibleDataRange);
  return model;
}

export function zoomLogicalRangeAtAnchor(
  current: { from: number; to: number },
  anchorLogical: number,
  zoomFactor: number,
  limits: { minBars: number; maxBars: number },
  totalBars: number
): { from: number; to: number } {
  const currentSpan = Math.max(1, current.to - current.from + 1);
  const nextSpan = clamp(currentSpan * zoomFactor, limits.minBars, Math.max(limits.minBars, Math.min(limits.maxBars, totalBars || limits.maxBars)));
  const anchorRatio = currentSpan <= 1 ? 0.5 : clamp((anchorLogical - current.from) / currentSpan, 0, 1);
  let from = anchorLogical - nextSpan * anchorRatio;
  let to = from + nextSpan - 1;
  if (totalBars > 0) {
    if (from < 0) {
      to -= from;
      from = 0;
    }
    if (to > totalBars - 1) {
      const overflow = to - (totalBars - 1);
      from = Math.max(0, from - overflow);
      to = totalBars - 1;
    }
  }
  return { from, to };
}

export function panLogicalRange(
  current: { from: number; to: number },
  deltaBars: number,
  totalBars: number
): { from: number; to: number } {
  const span = Math.max(1, current.to - current.from + 1);
  let from = current.from + deltaBars;
  let to = current.to + deltaBars;
  if (totalBars > 0) {
    if (from < 0) {
      from = 0;
      to = span - 1;
    }
    if (to > totalBars - 1) {
      to = totalBars - 1;
      from = Math.max(0, to - span + 1);
    }
  }
  return { from, to };
}

export function rightOffsetForLogicalRange(totalBars: number, range: { to: number }): number {
  if (totalBars <= 0) return 0;
  return Math.max(0, Math.round(totalBars - 1 - range.to));
}

function buildTimeTicks(
  scale: TimeScaleModel,
  visibleCandles: Candle[],
  visibleDataRange: { from: number; toExclusive: number }
): TimeAxisTick[] {
  if (visibleCandles.length === 0) return [];
  const desiredTickCount = Math.max(2, Math.floor(scale.bounds.width / 120));
  const step = Math.max(1, Math.ceil(visibleCandles.length / desiredTickCount));
  const ticks: TimeAxisTick[] = [];
  for (let visibleIndex = 0; visibleIndex < visibleCandles.length; visibleIndex += step) {
    const candle = visibleCandles[visibleIndex];
    const logical = visibleDataRange.from + visibleIndex;
    ticks.push({
      logical,
      x: scale.logicalToX(logical),
      timestamp: candle.timestamp,
      label: formatTimeTick(candle.timestamp)
    });
  }
  const last = visibleCandles[visibleCandles.length - 1];
  const lastLogical = visibleDataRange.toExclusive - 1;
  if (last && ticks[ticks.length - 1]?.timestamp !== last.timestamp) {
    ticks.push({
      logical: lastLogical,
      x: scale.logicalToX(lastLogical),
      timestamp: last.timestamp,
      label: formatTimeTick(last.timestamp)
    });
  }
  return ticks;
}

function formatTimeTick(timestamp: string): string {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return timestamp.slice(11, 16) || timestamp;
  return `${String(date.getUTCHours()).padStart(2, "0")}:${String(date.getUTCMinutes()).padStart(2, "0")}`;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
