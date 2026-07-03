import type { ChartLineExtension } from "./types";

export type DrawingPoint = { x: number; y: number };

export type PlotBounds = {
  left: number;
  right: number;
  top: number;
  priceBottom: number;
};

export function normalizeLineExtension(extension: unknown): ChartLineExtension {
  return extension === "ray" || extension === "line" ? extension : "segment";
}

export function projectTrendLine(
  start: DrawingPoint,
  end: DrawingPoint,
  plot: PlotBounds,
  extension: ChartLineExtension = "segment"
): [DrawingPoint, DrawingPoint] {
  if (extension === "segment") {
    return [start, end];
  }

  const dx = end.x - start.x;
  const dy = end.y - start.y;
  if (dx === 0 && dy === 0) {
    return [start, end];
  }

  const candidates = linePlotIntersections(start, dx, dy, plot);
  if (candidates.length === 0) {
    return [start, end];
  }

  if (extension === "ray") {
    const forward = candidates.filter((candidate) => candidate.t >= 0).sort((left, right) => left.t - right.t);
    const far = forward[forward.length - 1];
    return far ? [start, far.point] : [start, end];
  }

  const sorted = candidates.sort((left, right) => left.t - right.t);
  return [sorted[0].point, sorted[sorted.length - 1].point];
}

function linePlotIntersections(
  start: DrawingPoint,
  dx: number,
  dy: number,
  plot: PlotBounds
): Array<{ t: number; point: DrawingPoint }> {
  const candidates: Array<{ t: number; point: DrawingPoint }> = [];
  const addCandidate = (t: number, point: DrawingPoint) => {
    if (
      Number.isFinite(t) &&
      point.x >= plot.left - 0.5 &&
      point.x <= plot.right + 0.5 &&
      point.y >= plot.top - 0.5 &&
      point.y <= plot.priceBottom + 0.5 &&
      !candidates.some((candidate) => Math.abs(candidate.point.x - point.x) < 0.5 && Math.abs(candidate.point.y - point.y) < 0.5)
    ) {
      candidates.push({ t, point });
    }
  };

  if (dx !== 0) {
    const leftT = (plot.left - start.x) / dx;
    addCandidate(leftT, { x: plot.left, y: start.y + leftT * dy });
    const rightT = (plot.right - start.x) / dx;
    addCandidate(rightT, { x: plot.right, y: start.y + rightT * dy });
  }

  if (dy !== 0) {
    const topT = (plot.top - start.y) / dy;
    addCandidate(topT, { x: start.x + topT * dx, y: plot.top });
    const bottomT = (plot.priceBottom - start.y) / dy;
    addCandidate(bottomT, { x: start.x + bottomT * dx, y: plot.priceBottom });
  }

  return candidates;
}
