import type { ChartViewport } from "../types/documents";

export interface VisibleIndexRange {
  from: number;
  toExclusive: number;
  logicalFrom: number;
  logicalTo: number;
  visibleBars: number;
}

export function resolveVisibleIndexRange(totalBars: number, viewport: ChartViewport): VisibleIndexRange {
  if (totalBars <= 0) {
    return { from: 0, toExclusive: 0, logicalFrom: 0, logicalTo: 0, visibleBars: 0 };
  }

  if (viewport.mode === "fixedLogicalRange" && isFiniteNumber(viewport.logicalFrom) && isFiniteNumber(viewport.logicalTo)) {
    const logicalFrom = Math.min(Number(viewport.logicalFrom), Number(viewport.logicalTo));
    const logicalTo = Math.max(Number(viewport.logicalFrom), Number(viewport.logicalTo));
    const from = clamp(Math.floor(logicalFrom), 0, totalBars - 1);
    const toExclusive = clamp(Math.ceil(logicalTo) + 1, from + 1, totalBars);
    return {
      from,
      toExclusive,
      logicalFrom,
      logicalTo,
      visibleBars: Math.max(1, toExclusive - from)
    };
  }

  const minVisibleBars = viewport.minVisibleBars ?? 20;
  const maxVisibleBars = viewport.maxVisibleBars ?? 1000;
  const visibleBars = clamp(Math.round(viewport.visibleBars), minVisibleBars, maxVisibleBars);
  const rightOffset = clamp(Math.round(viewport.rightOffsetBars), 0, Math.max(0, totalBars - visibleBars));
  const toExclusive = Math.max(0, totalBars - rightOffset);
  const from = Math.max(0, toExclusive - visibleBars);
  return {
    from,
    toExclusive,
    logicalFrom: from,
    logicalTo: Math.max(from, toExclusive - 1),
    visibleBars: Math.max(1, toExclusive - from)
  };
}

export function visibleBarLimit(viewport: ChartViewport): { min: number; max: number } {
  return {
    min: viewport.minVisibleBars ?? 20,
    max: viewport.maxVisibleBars ?? 1000
  };
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
