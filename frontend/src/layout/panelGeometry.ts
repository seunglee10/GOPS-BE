export type LayoutRect = {
  left: number;
  top: number;
  width: number;
  height: number;
};

const defaultTolerance = 0.5;

export function rectRight(rect: LayoutRect): number {
  return rect.left + rect.width;
}

export function rectBottom(rect: LayoutRect): number {
  return rect.top + rect.height;
}

export function rangesOverlap(aStart: number, aEnd: number, bStart: number, bEnd: number, tolerance = 0): boolean {
  return Math.min(aEnd, bEnd) - Math.max(aStart, bStart) > tolerance;
}

export function rectsOverlap(a: LayoutRect, b: LayoutRect, tolerance = 0): boolean {
  return rangesOverlap(a.left, rectRight(a), b.left, rectRight(b), tolerance) &&
    rangesOverlap(a.top, rectBottom(a), b.top, rectBottom(b), tolerance);
}

export function almostEqual(a: number, b: number, tolerance = defaultTolerance): boolean {
  return Math.abs(a - b) <= tolerance;
}

export function uniqueStrings(values: string[]): string[] {
  return [...new Set(values)].sort();
}

export function sortedUnique(values: number[]): number[] {
  return [...new Set(values.map((value) => Math.round(value * 1000) / 1000))].sort((a, b) => a - b);
}

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
