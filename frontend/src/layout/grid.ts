export type GridLineIndex = 0 | 1 | 2 | 3 | 4;

export type PanelGridSpan = {
  start: GridLineIndex;
  end: GridLineIndex;
};

export type PanelEdgeBehavior = "normal" | "flush-at-page-edge";

export type PanelGridBounds = {
  left: number;
  right: number;
  width: number;
};

const columnCount = 4;
export const gridLockMaxWidth = 760;
export const defaultChartGridSpan: PanelGridSpan = { start: 0, end: 4 };

export function gridGutter(viewportWidth: number): number {
  return Math.round(Math.min(18, Math.max(10, viewportWidth * 0.012)));
}

export function gridBoundaryX(line: GridLineIndex, viewportWidth: number): number {
  return Math.round((viewportWidth / columnCount) * line);
}

export function isGridLocked(viewportWidth: number): boolean {
  return viewportWidth <= gridLockMaxWidth;
}

export function normalizeGridSpan(span: PanelGridSpan): PanelGridSpan {
  const start = clampGridLine(span.start);
  const end = clampGridLine(span.end);
  if (start >= 4) {
    return { start: 3, end: 4 };
  }
  if (end <= start) {
    return { start, end: clampGridLine(start + 1) };
  }
  return { start, end };
}

export function panelGridBounds(
  span: PanelGridSpan,
  viewportWidth: number,
  edgeBehavior: PanelEdgeBehavior
): PanelGridBounds {
  const normalized = normalizeGridSpan(span);
  const gutter = gridGutter(viewportWidth);
  const left = edgeLeft(normalized.start, viewportWidth, gutter, edgeBehavior);
  const right = edgeRight(normalized.end, viewportWidth, gutter, edgeBehavior);
  return {
    left,
    right,
    width: Math.max(1, right - left)
  };
}

function edgeLeft(
  line: GridLineIndex,
  viewportWidth: number,
  gutter: number,
  edgeBehavior: PanelEdgeBehavior
): number {
  if (edgeBehavior === "flush-at-page-edge" && line === 0) {
    return 0;
  }
  return gridBoundaryX(line, viewportWidth) + gutter;
}

function edgeRight(
  line: GridLineIndex,
  viewportWidth: number,
  gutter: number,
  edgeBehavior: PanelEdgeBehavior
): number {
  if (edgeBehavior === "flush-at-page-edge" && line === 4) {
    return viewportWidth;
  }
  return gridBoundaryX(line, viewportWidth) - gutter;
}

function clampGridLine(value: number): GridLineIndex {
  return Math.max(0, Math.min(4, Math.round(value))) as GridLineIndex;
}
