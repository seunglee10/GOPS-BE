import type {
  ChartLineExtension,
  ChartToolMode,
  DrawingAnchor,
  DrawingEntity,
  DrawingStyle,
  DrawingType
} from "./types";
import type { ChartScene } from "./scene";
import { createCoordinateTransform } from "./scene";

export type DrawingDraft = {
  type: DrawingType;
  first: DrawingAnchor;
};

export type DrawingDrag = {
  drawing: DrawingEntity;
  anchor: DrawingAnchor;
  anchorIndex: number | null;
};

export const drawingTools: Array<{ mode: ChartToolMode; type?: DrawingType; label: string }> = [
  { mode: "select", label: "Select" },
  { mode: "pan", label: "Pan" },
  { mode: "draw-horizontalLine", type: "horizontalLine", label: "H-Line" },
  { mode: "draw-verticalMarker", type: "verticalMarker", label: "Marker" },
  { mode: "draw-trendLine", type: "trendLine", label: "Trend" },
  { mode: "draw-textLabel", type: "textLabel", label: "Text" },
  { mode: "draw-pointMarker", type: "pointMarker", label: "Point" },
  { mode: "draw-arrow", type: "arrow", label: "Arrow" },
  { mode: "draw-rangeBox", type: "rangeBox", label: "Range" },
  { mode: "draw-measurement", type: "measurement", label: "Measure" }
];

export function drawingTypeFromToolMode(mode: ChartToolMode): DrawingType | null {
  const tool = drawingTools.find((item) => item.mode === mode);
  return tool?.type ?? null;
}

export function drawingNeedsTwoAnchors(type: DrawingType): boolean {
  return type === "trendLine" || type === "arrow" || type === "rangeBox" || type === "measurement";
}

export function makeDrawing(
  type: DrawingType,
  anchors: DrawingAnchor[],
  options: {
    trendLineExtension?: ChartLineExtension;
    createdBy?: "user" | "agent";
    style?: DrawingStyle;
    label?: string;
  } = {}
): DrawingEntity {
  const now = new Date().toISOString();
  return {
    id: `drawing-${crypto.randomUUID()}`,
    type,
    anchors,
    style: options.style ?? defaultDrawingStyle(type, options.trendLineExtension),
    label: options.label ?? defaultDrawingLabel(type),
    visible: true,
    createdBy: options.createdBy ?? "user",
    createdAt: now,
    updatedAt: now
  };
}

export function buildDraftPreviewDrawing(draft: DrawingDraft, anchor: DrawingAnchor, trendLineExtension: ChartLineExtension): DrawingEntity {
  return {
    id: "drawing-draft-preview",
    type: draft.type,
    anchors: [draft.first, anchor],
    style: { ...defaultDrawingStyle(draft.type, trendLineExtension), opacity: 0.58, lineDash: [6, 4] },
    label: defaultDrawingLabel(draft.type),
    visible: true,
    createdBy: "user",
    createdAt: "draft",
    updatedAt: "draft"
  };
}

export function buildSingleAnchorPreviewDrawing(type: DrawingType, anchor: DrawingAnchor, trendLineExtension: ChartLineExtension): DrawingEntity {
  return {
    id: "drawing-draft-preview",
    type,
    anchors: [anchor],
    style: { ...defaultDrawingStyle(type, trendLineExtension), opacity: 0.46, lineDash: [5, 5] },
    label: defaultDrawingLabel(type),
    visible: true,
    createdBy: "user",
    createdAt: "draft",
    updatedAt: "draft"
  };
}

export function defaultDrawingStyle(type: DrawingType, trendLineExtension: ChartLineExtension = "segment"): DrawingStyle {
  if (type === "rangeBox") {
    return { colorToken: "preview", fillToken: "preview", fillOpacity: 0.12, lineWidth: 1.4 };
  }
  if (type === "trendLine") {
    return { colorToken: "drawing", lineWidth: 1.5, extension: trendLineExtension };
  }
  if (type === "measurement") {
    return { colorToken: "ma20", textToken: "ma20", lineWidth: 1.4 };
  }
  if (type === "arrow") {
    return { colorToken: "ma60", lineWidth: 1.6 };
  }
  return { colorToken: "drawing", lineWidth: 1.5 };
}

export function defaultDrawingLabel(type?: DrawingType): string | undefined {
  switch (type) {
    case "horizontalLine":
      return "기준선";
    case "verticalMarker":
      return "이벤트";
    case "textLabel":
      return "메모";
    case "pointMarker":
      return "포인트";
    case "rangeBox":
      return "범위";
    case "measurement":
      return "측정";
    default:
      return undefined;
  }
}

export function normalizeLineExtension(extension: unknown): ChartLineExtension {
  return extension === "ray" || extension === "line" ? extension : "segment";
}

export function projectTrendLine(
  start: { x: number; y: number },
  end: { x: number; y: number },
  plot: { left: number; right: number; top: number; priceBottom: number },
  extension: ChartLineExtension = "segment"
): [{ x: number; y: number }, { x: number; y: number }] {
  if (extension === "segment") {
    return [start, end];
  }
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  if (dx === 0 && dy === 0) {
    return [start, end];
  }
  const candidates = linePlotIntersections(start, dx, dy, plot);
  if (!candidates.length) {
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

export function buildDraggedAnchors(drag: DrawingDrag, anchor: DrawingAnchor, scene: ChartScene): DrawingAnchor[] {
  return drag.drawing.anchors.map((item, index) => {
    if (drag.anchorIndex !== null) {
      return index === drag.anchorIndex ? anchor : item;
    }
    const priceDelta = (anchor.price ?? 0) - (drag.anchor.price ?? 0);
    const logicalDelta = (anchor.logicalIndex ?? 0) - (drag.anchor.logicalIndex ?? 0);
    const nextLogical = typeof item.logicalIndex === "number" ? item.logicalIndex + logicalDelta : item.logicalIndex;
    const nextTimestamp = typeof nextLogical === "number"
      ? scene.allCandles[Math.max(0, Math.min(scene.allCandles.length - 1, Math.round(nextLogical)))]?.timestamp ?? item.timestamp
      : item.timestamp;
    return {
      ...item,
      logicalIndex: nextLogical,
      timestamp: nextTimestamp,
      price: typeof item.price === "number" ? item.price + priceDelta : item.price
    };
  });
}

export function hitTestDrawing(scene: ChartScene, x: number, y: number): { drawing: DrawingEntity; anchorIndex: number | null } | null {
  const transform = createCoordinateTransform(scene);
  for (const drawing of [...scene.chart.drawings].reverse()) {
    if (drawing.visible === false) {
      continue;
    }
    const points = drawing.anchors.map((anchor) => transform.anchorToPoint(anchor)).filter((point): point is { x: number; y: number } => Boolean(point));
    const anchorIndex = points.findIndex((point) => distance(point.x, point.y, x, y) <= 8);
    if (anchorIndex >= 0) {
      return { drawing, anchorIndex };
    }
    if (drawing.type === "horizontalLine" && points[0] && Math.abs(points[0].y - y) <= 6 && x >= scene.plot.left && x <= scene.plot.right) {
      return { drawing, anchorIndex: null };
    }
    if (drawing.type === "verticalMarker" && points[0] && Math.abs(points[0].x - x) <= 6 && y >= scene.plot.top && y <= scene.plot.priceBottom) {
      return { drawing, anchorIndex: null };
    }
    if ((drawing.type === "trendLine" || drawing.type === "arrow" || drawing.type === "measurement") && points.length >= 2) {
      const [start, end] = drawing.type === "trendLine"
        ? projectTrendLine(points[0], points[1], scene.plot, normalizeLineExtension(drawing.style.extension))
        : [points[0], points[1]];
      if (distanceToSegment(x, y, start, end) <= 7) {
        return { drawing, anchorIndex: null };
      }
    }
    if (drawing.type === "rangeBox" && points.length >= 2) {
      const left = Math.min(points[0].x, points[1].x);
      const right = Math.max(points[0].x, points[1].x);
      const top = Math.min(points[0].y, points[1].y);
      const bottom = Math.max(points[0].y, points[1].y);
      if (x >= left && x <= right && y >= top && y <= bottom) {
        return { drawing, anchorIndex: null };
      }
    }
    if ((drawing.type === "pointMarker" || drawing.type === "textLabel") && points[0] && distance(points[0].x, points[0].y, x, y) <= 12) {
      return { drawing, anchorIndex: null };
    }
  }
  return null;
}

function linePlotIntersections(
  start: { x: number; y: number },
  dx: number,
  dy: number,
  plot: { left: number; right: number; top: number; priceBottom: number }
): Array<{ t: number; point: { x: number; y: number } }> {
  const candidates: Array<{ t: number; point: { x: number; y: number } }> = [];
  const addCandidate = (t: number, point: { x: number; y: number }) => {
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

function distance(x1: number, y1: number, x2: number, y2: number): number {
  return Math.hypot(x2 - x1, y2 - y1);
}

function distanceToSegment(x: number, y: number, start: { x: number; y: number }, end: { x: number; y: number }): number {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared === 0) {
    return distance(x, y, start.x, start.y);
  }
  const t = Math.max(0, Math.min(1, ((x - start.x) * dx + (y - start.y) * dy) / lengthSquared));
  return distance(x, y, start.x + t * dx, start.y + t * dy);
}
