import type { PointerEventHandler, WheelEventHandler } from "react";
import { useEffect, useRef } from "react";
import type { ChartState, DrawingEntity } from "./types";
import { buildChartScene, createCoordinateTransform, hitTestSemanticNode, unitBoundsX, unitCenterX, type ChartScene } from "./scene";
import { normalizeLineExtension, projectTrendLine } from "./drawings";
import { expansionMetadataTop, expansionParentCandleHeight, expansionParentCandleWidth, expansionSummaryVisibleBounds } from "./expansionLayout";
import { formatSemanticTimestamp, type SemanticCandleUnit, type SemanticExpansion, type SemanticRenderUnit } from "./semanticTimeline";

type ChartCanvasProps = {
  chart: ChartState;
  expansions?: SemanticExpansion[];
  previewDrawings?: DrawingEntity[];
  hoveredNodeId?: string;
  selectedNodeId?: string;
  crosshair?: { x: number; y: number };
  onScene?: (scene: ChartScene) => void;
  onWheel?: WheelEventHandler<HTMLCanvasElement>;
  onPointerDown?: PointerEventHandler<HTMLCanvasElement>;
  onPointerMove?: PointerEventHandler<HTMLCanvasElement>;
  onPointerLeave?: PointerEventHandler<HTMLCanvasElement>;
  onPointerUp?: PointerEventHandler<HTMLCanvasElement>;
  onPointerCancel?: PointerEventHandler<HTMLCanvasElement>;
  onLostPointerCapture?: PointerEventHandler<HTMLCanvasElement>;
};

const colors = {
  grid: "rgba(129, 145, 168, 0.22)",
  priceBoundary: "rgba(83, 97, 118, 0.36)",
  text: "#344256",
  muted: "rgba(83, 97, 118, 0.72)",
  bullish: "#168c68",
  bearish: "#c94b4b",
  volume: "rgba(77, 96, 130, 0.16)",
  ma5: "#2563eb",
  ma20: "#a855f7",
  ma60: "#f59e0b",
  drawing: "#0f172a",
  preview: "#2563eb"
};

export function ChartCanvas({
  chart,
  expansions = [],
  previewDrawings = [],
  hoveredNodeId,
  selectedNodeId,
  crosshair,
  onScene,
  onWheel,
  onPointerDown,
  onPointerMove,
  onPointerLeave,
  onPointerUp,
  onPointerCancel,
  onLostPointerCapture
}: ChartCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }

    const resize = () => {
      const rect = canvas.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * ratio));
      canvas.height = Math.max(1, Math.floor(rect.height * ratio));
      const context = canvas.getContext("2d");
      if (!context) {
        return;
      }
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      const chartForScene = previewDrawings.length
        ? { ...chart, drawings: [...chart.drawings, ...previewDrawings] }
        : chart;
      const sceneForScale = buildChartScene(chartForScene, rect.width, rect.height, { expansions, hoveredNodeId, selectedNodeId });
      const scene = previewDrawings.length ? { ...sceneForScale, chart } : sceneForScale;
      onScene?.(scene);
      drawChart(context, scene, crosshair, previewDrawings);
    };

    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    resize();
    return () => observer.disconnect();
  }, [chart, crosshair, expansions, hoveredNodeId, onScene, previewDrawings, selectedNodeId]);

  return (
    <canvas
      ref={canvasRef}
      className="chart-canvas"
      aria-label="GOPS candle chart"
      onWheel={onWheel}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerLeave={onPointerLeave}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onLostPointerCapture={onLostPointerCapture}
    />
  );
}

type RenderLayer = {
  id: string;
  draw: () => void;
};

type SemanticVisualRole = "normal" | "hover-target" | "hover-context";

type SemanticVisualStyle = {
  role: SemanticVisualRole;
  opacity: number;
};

function drawChart(
  context: CanvasRenderingContext2D,
  scene: ChartScene,
  crosshair?: { x: number; y: number },
  previewDrawings: DrawingEntity[] = []
) {
  context.clearRect(0, 0, scene.width, scene.height);

  if (!scene.candles.length && !scene.semantic.units.length) {
    drawEmpty(context, scene.width, scene.height, scene.chart.message ?? "Waiting for chart data");
    return;
  }

  const layers: RenderLayer[] = [
    { id: "expansion-background", draw: () => drawExpansionRanges(context, scene, Boolean(crosshair)) },
    { id: "time-grid", draw: () => drawTimeGrid(context, scene) },
    { id: "price-grid", draw: () => drawGrid(context, scene) },
    { id: "volume", draw: () => hasVolumePane(scene) && drawPlotClipped(context, scene, () => drawVolume(context, scene)) },
    { id: "candles", draw: () => scene.chart.layers.candles && drawPlotClipped(context, scene, () => drawCandles(context, scene)) },
    { id: "ma5", draw: () => drawPlotClipped(context, scene, () => drawMovingAverage(context, scene, "ma5", scene.chart.layers.ma5, colors.ma5)) },
    { id: "ma20", draw: () => drawPlotClipped(context, scene, () => drawMovingAverage(context, scene, "ma20", scene.chart.layers.ma20, colors.ma20)) },
    { id: "ma60", draw: () => drawPlotClipped(context, scene, () => drawMovingAverage(context, scene, "ma60", scene.chart.layers.ma60, colors.ma60)) },
    { id: "drawings", draw: () => drawDrawings(context, scene, scene.chart.drawings, false) },
    { id: "agent-preview", draw: () => drawDrawings(context, scene, previewDrawings, true) },
    { id: "expansion-summary", draw: () => drawExpansionParentSummaries(context, scene) },
    { id: "time-axis", draw: () => drawAxes(context, scene) },
    { id: "price-axis", draw: () => drawPriceAxis(context, scene) },
    { id: "crosshair", draw: () => drawCrosshair(context, scene, crosshair) }
  ];
  layers.forEach((layer) => layer.draw());
}

function drawPlotClipped(context: CanvasRenderingContext2D, scene: ChartScene, draw: () => void) {
  context.save();
  context.beginPath();
  context.rect(scene.plot.left, 0, Math.max(1, scene.plot.right - scene.plot.left), scene.height);
  context.clip();
  draw();
  context.restore();
}

function priceY(scene: ChartScene, value: number): number {
  const range = Math.max(0.0001, scene.scales.maxPrice - scene.scales.minPrice);
  return scene.plot.top + ((scene.scales.maxPrice - value) / range) * Math.max(1, scene.plot.priceBottom - scene.plot.top);
}

function volumeY(scene: ChartScene, value: number): number {
  const range = Math.max(1, scene.scales.maxVolume);
  const height = Math.max(1, scene.plot.bottom - scene.plot.volumeTop);
  return scene.plot.bottom - (Math.max(0, value) / range) * height;
}

function volumeAtY(scene: ChartScene, y: number): number {
  const range = Math.max(1, scene.scales.maxVolume);
  const height = Math.max(1, scene.plot.bottom - scene.plot.volumeTop);
  const clampedY = Math.max(scene.plot.volumeTop, Math.min(scene.plot.bottom, y));
  return ((scene.plot.bottom - clampedY) / height) * range;
}

function drawGrid(context: CanvasRenderingContext2D, scene: ChartScene) {
  context.strokeStyle = colors.grid;
  context.lineWidth = 1;
  const right = horizontalGuideRight(scene);
  scene.scales.priceTicks.forEach((price) => {
    const y = priceY(scene, price);
    line(context, scene.plot.left, y, right, y);
  });
  if (hasVolumePane(scene)) {
    scene.scales.volumeTicks.forEach((volume) => {
      line(context, scene.plot.left, volumeY(scene, volume), right, volumeY(scene, volume));
    });
    context.save();
    context.strokeStyle = colors.priceBoundary;
    context.lineWidth = 1.2;
    line(context, scene.plot.left, scene.plot.priceBottom, right, scene.plot.priceBottom);
    context.restore();
  }
}

function drawCandles(context: CanvasRenderingContext2D, scene: ChartScene) {
  candleUnits(scene).forEach((unit) => {
    const candle = unit.candle;
    const center = unitCenterX(scene, unit);
    const open = priceY(scene, candle.open);
    const close = priceY(scene, candle.close);
    const high = priceY(scene, candle.high);
    const low = priceY(scene, candle.low);
    const up = candle.close >= candle.open;
    context.save();
    applySemanticVisualStyle(context, semanticVisualStyle(scene, unit));
    context.strokeStyle = up ? colors.bullish : colors.bearish;
    context.fillStyle = up ? colors.bullish : colors.bearish;
    context.lineWidth = 1.25;
    const bodyTop = Math.min(open, close);
    const bodyHeight = Math.max(2, Math.abs(close - open));
    const bodyBottom = bodyTop + bodyHeight;
    line(context, center, high, center, bodyTop);
    line(context, center, bodyBottom, center, low);
    context.fillRect(center - scene.scales.candleWidth / 2, bodyTop, scene.scales.candleWidth, bodyHeight);
    context.restore();
  });
}

function drawVolume(context: CanvasRenderingContext2D, scene: ChartScene) {
  candleUnits(scene).forEach((unit) => {
    const candle = unit.candle;
    const y = volumeY(scene, candle.volume);
    context.save();
    applySemanticVisualStyle(context, semanticVisualStyle(scene, unit));
    context.fillStyle = colors.volume;
    context.fillRect(
      unitCenterX(scene, unit) - scene.scales.candleWidth / 2,
      y,
      scene.scales.candleWidth,
      scene.plot.bottom - y
    );
    context.restore();
  });
}

function drawMovingAverage(
  context: CanvasRenderingContext2D,
  scene: ChartScene,
  key: "ma5" | "ma20" | "ma60",
  enabled: boolean,
  stroke: string
) {
  if (!enabled) {
    return;
  }
  context.strokeStyle = stroke;
  context.lineWidth = 1.7;
  let started = false;
  let lastSegmentKey = "";
  context.beginPath();
  candleUnits(scene).forEach((unit) => {
    const candle = unit.candle;
    const value = candle[key];
    const segmentKey = `${unit.parentExpansionId ?? "root"}:${unit.interval}`;
    if (typeof value !== "number") {
      if (started) {
        context.stroke();
        context.beginPath();
        started = false;
      }
      return;
    }
    if (started && segmentKey !== lastSegmentKey) {
      context.stroke();
      context.beginPath();
      started = false;
    }
    const x = unitCenterX(scene, unit);
    const y = priceY(scene, value);
    if (!started) {
      context.moveTo(x, y);
      started = true;
    } else {
      context.lineTo(x, y);
    }
    lastSegmentKey = segmentKey;
  });
  if (started) {
    context.stroke();
  }
}

function drawDrawings(context: CanvasRenderingContext2D, scene: ChartScene, drawings: DrawingEntity[], previewLayer: boolean) {
  const transform = createCoordinateTransform(scene);
  drawings.filter((drawing) => drawing.visible !== false).forEach((drawing) => {
    const selected = !previewLayer && scene.chart.selectedDrawingId === drawing.id;
    const preview = previewLayer || drawing.id === "drawing-draft-preview";
    const style = drawing.style ?? {};
    const points = drawing.anchors.map((anchor) => transform.anchorToPoint(anchor)).filter((point): point is { x: number; y: number } => Boolean(point));

    context.save();
    context.globalAlpha = preview ? 0.58 : style.opacity ?? 1;
    context.strokeStyle = style.color ?? (preview ? colors.preview : colors.drawing);
    context.fillStyle = style.fillColor ?? (preview ? "rgba(37, 99, 235, 0.08)" : "rgba(17, 17, 17, 0.08)");
    context.lineWidth = selected ? Math.max(2.2, style.lineWidth ?? 1.5) : style.lineWidth ?? 1.5;
    context.setLineDash(preview ? [6, 4] : style.lineDash ?? []);

    if (drawing.type === "horizontalLine" && points[0]) {
      line(context, scene.plot.left, points[0].y, horizontalGuideRight(scene), points[0].y);
      drawDrawingLabel(context, drawing.label, scene.plot.right - 54, points[0].y - 8, drawing);
    } else if (drawing.type === "verticalMarker" && points[0]) {
      line(context, points[0].x, scene.plot.top, points[0].x, scene.plot.priceBottom);
      drawDrawingLabel(context, drawing.label, points[0].x + 5, scene.plot.top + 12, drawing);
    } else if ((drawing.type === "trendLine" || drawing.type === "arrow" || drawing.type === "measurement") && points.length >= 2) {
      const [start, end] = drawing.type === "trendLine"
        ? projectTrendLine(points[0], points[1], scene.plot, normalizeLineExtension(style.extension))
        : [points[0], points[1]];
      line(context, start.x, start.y, end.x, end.y);
      if (drawing.type === "arrow") {
        drawArrowHead(context, start, end);
      }
      drawDrawingLabel(context, drawing.label ?? measurementLabel(drawing, scene), (start.x + end.x) / 2, (start.y + end.y) / 2 - 8, drawing);
    } else if (drawing.type === "rangeBox" && points.length >= 2) {
      const x = Math.min(points[0].x, points[1].x);
      const y = Math.min(points[0].y, points[1].y);
      const width = Math.abs(points[1].x - points[0].x);
      const height = Math.abs(points[1].y - points[0].y);
      context.fillRect(x, y, width, height);
      context.strokeRect(x, y, width, height);
      drawDrawingLabel(context, drawing.label, x + 5, y + 13, drawing);
    } else if ((drawing.type === "pointMarker" || drawing.type === "textLabel") && points[0]) {
      circle(context, points[0].x, points[0].y, drawing.type === "pointMarker" ? 4 : 3);
      context.fill();
      drawDrawingLabel(context, drawing.label ?? (drawing.type === "textLabel" ? "메모" : ""), points[0].x + 7, points[0].y - 7, drawing);
    }

    if (selected && points.length) {
      context.setLineDash([]);
      context.fillStyle = "#ffffff";
      context.strokeStyle = "#111111";
      points.forEach((point) => {
        circle(context, point.x, point.y, 4);
        context.fill();
        context.stroke();
      });
    }
    context.restore();
  });
}

function drawDrawingLabel(context: CanvasRenderingContext2D, label: string | undefined, x: number, y: number, drawing: DrawingEntity) {
  if (!label) {
    return;
  }
  const style = drawing.style ?? {};
  context.fillStyle = style.textColor ?? style.color ?? colors.drawing;
  context.font = `${style.fontSize ?? 11}px Inter, system-ui, sans-serif`;
  context.textAlign = "left";
  context.textBaseline = "middle";
  context.fillText(label, x, y);
}

function measurementLabel(drawing: DrawingEntity, scene: ChartScene): string {
  const [start, end] = drawing.anchors;
  if (typeof start?.price !== "number" || typeof end?.price !== "number") {
    return drawing.label ?? "측정";
  }
  const delta = end.price - start.price;
  const percent = (delta / Math.max(0.0001, start.price)) * 100;
  const bars = typeof start.logicalIndex === "number" && typeof end.logicalIndex === "number"
    ? Math.abs(end.logicalIndex - start.logicalIndex)
    : 0;
  return `${delta >= 0 ? "+" : ""}${delta.toFixed(2)} / ${percent >= 0 ? "+" : ""}${percent.toFixed(2)}% / ${bars}봉`;
}

type TimeTick = {
  x: number;
  label: string;
  depth: number;
  parentExpansionId?: string;
};

function drawTimeGrid(context: CanvasRenderingContext2D, scene: ChartScene) {
  const ticks = buildTimeTicks(scene);
  if (!ticks.length) {
    return;
  }
  context.save();
  context.strokeStyle = "rgba(99, 116, 139, 0.11)";
  context.lineWidth = 1;
  ticks.forEach((tick) => {
    const alpha = tick.parentExpansionId ? 0.14 : 0.1;
    context.strokeStyle = `rgba(99, 116, 139, ${alpha})`;
    line(context, tick.x, scene.plot.top, tick.x, Math.min(scene.height, timeAxisY(scene) - 10));
  });
  context.restore();
}

function drawAxes(context: CanvasRenderingContext2D, scene: ChartScene) {
  const ticks = buildTimeTicks(scene);
  if (!ticks.length) {
    return;
  }
  context.fillStyle = colors.muted;
  context.font = "10px Inter, system-ui, sans-serif";
  context.textBaseline = "middle";
  context.textAlign = "center";
  const y = timeAxisY(scene);
  let lastLabelX = Number.NEGATIVE_INFINITY;
  ticks.forEach((tick) => {
    const x = Math.max(scene.plot.left + 24, Math.min(scene.plot.right - 24, tick.x));
    const minimumGap = tick.parentExpansionId ? 46 : 62;
    if (x - lastLabelX < minimumGap) {
      return;
    }
    lastLabelX = x;
    context.fillStyle = tick.parentExpansionId ? "rgba(58, 72, 91, 0.74)" : colors.muted;
    context.fillText(tick.label, x, y);
  });
}

function buildTimeTicks(scene: ChartScene): TimeTick[] {
  const ticks: TimeTick[] = [];
  const lastXByGroup = new Map<string, number>();
  const candles = candleUnits(scene);
  candles.forEach((unit, index) => {
    const x = unitCenterX(scene, unit);
    if (x < scene.plot.left - 1 || x > scene.plot.right + 1) {
      return;
    }
    if (!shouldShowTimeTick(unit, scene.scales.slotWidth, index === 0 || index === candles.length - 1)) {
      return;
    }
    const group = unit.parentExpansionId ?? "root";
    const minGap = unit.parentExpansionId ? 42 : 68;
    const lastX = lastXByGroup.get(group);
    if (typeof lastX === "number" && x - lastX < minGap) {
      return;
    }
    lastXByGroup.set(group, x);
    ticks.push({
      x,
      label: formatAxisTimestamp(unit.timestamp, unit.interval),
      depth: unit.depth,
      parentExpansionId: unit.parentExpansionId
    });
  });

  if (!ticks.length && candles.length) {
    const first = candles[0];
    const last = candles[candles.length - 1];
    ticks.push({
      x: unitCenterX(scene, first),
      label: formatAxisTimestamp(first.timestamp, first.interval),
      depth: first.depth,
      parentExpansionId: first.parentExpansionId
    });
    if (last !== first) {
      ticks.push({
        x: unitCenterX(scene, last),
        label: formatAxisTimestamp(last.timestamp, last.interval),
        depth: last.depth,
        parentExpansionId: last.parentExpansionId
      });
    }
  }
  return ticks.sort((left, right) => left.x - right.x);
}

function shouldShowTimeTick(unit: SemanticCandleUnit, slotWidth: number, edge: boolean): boolean {
  if (edge) {
    return true;
  }
  const date = new Date(unit.timestamp);
  if (Number.isNaN(date.getTime())) {
    return false;
  }
  const minute = date.getUTCMinutes();
  switch (unit.interval) {
    case "1m":
      return minute % (slotWidth > 10 ? 5 : 15) === 0;
    case "5m":
      return minute % (slotWidth > 11 ? 15 : 30) === 0;
    case "10m":
      return minute === 0 || (slotWidth > 12 && minute % 30 === 0);
    case "1D":
      return slotWidth > 18 || date.getUTCDay() === 1 || date.getUTCDate() <= 3;
    case "1W":
      return slotWidth > 20 || date.getUTCDate() <= 7;
    case "1M":
      return slotWidth > 24 || date.getUTCMonth() % 3 === 0;
  }
}

function formatAxisTimestamp(value: string, interval: SemanticCandleUnit["interval"]): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  if (interval === "1m" || interval === "5m" || interval === "10m") {
    return new Intl.DateTimeFormat("en-US", { hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
  }
  if (interval === "1M") {
    return new Intl.DateTimeFormat("en-US", { month: "short", year: "2-digit" }).format(date);
  }
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "2-digit" }).format(date);
}

function semanticVisualStyle(scene: ChartScene, unit: SemanticRenderUnit): SemanticVisualStyle {
  if (!scene.hoveredNodeId) {
    return { role: "normal", opacity: 1 };
  }
  return unit.id === scene.hoveredNodeId
    ? { role: "hover-target", opacity: 1 }
    : { role: "hover-context", opacity: 0.68 };
}

function applySemanticVisualStyle(context: CanvasRenderingContext2D, style: SemanticVisualStyle) {
  context.globalAlpha *= style.opacity;
}

function timeAxisY(scene: ChartScene): number {
  const target = hasVolumePane(scene) ? scene.plot.bottom + 20 : scene.plot.priceBottom + 20;
  return Math.min(scene.height - 15, Math.max(scene.plot.top + 22, target));
}

function drawPriceAxis(context: CanvasRenderingContext2D, scene: ChartScene) {
  drawPriceAxisBackdrop(context, scene);
  context.save();
  context.font = "10px Inter, system-ui, sans-serif";
  context.fillStyle = colors.muted;
  context.textAlign = "right";
  context.textBaseline = "middle";
  scene.scales.priceTicks.forEach((price) => {
    context.fillText(formatPriceAxisValue(price), scene.width - 8, priceY(scene, price));
  });
  drawVolumeAxisLabels(context, scene);
  context.restore();
}

function drawPriceAxisBackdrop(context: CanvasRenderingContext2D, scene: ChartScene) {
  void context;
  void scene;
}

function drawVolumeAxisLabels(context: CanvasRenderingContext2D, scene: ChartScene) {
  if (!hasVolumePane(scene)) {
    return;
  }
  context.save();
  context.font = "9px Inter, system-ui, sans-serif";
  context.fillStyle = "rgba(83, 97, 118, 0.64)";
  context.textAlign = "right";
  context.textBaseline = "middle";
  scene.scales.volumeTicks.forEach((volume) => {
    context.fillText(formatVolumeAxisValue(volume), scene.width - 8, volumeY(scene, volume));
  });
  context.restore();
}

function formatVolumeAxisValue(value: number): string {
  if (!Number.isFinite(value) || value <= 0) {
    return "0";
  }
  if (value < 1_000) {
    return Math.round(value).toLocaleString("en-US");
  }
  const unit = value >= 999_500 ? "M" : "K";
  const divisor = unit === "M" ? 1_000_000 : 1_000;
  return `${formatCompactVolumeNumber(value / divisor)}${unit}`;
}

function formatCompactVolumeNumber(value: number): string {
  const fractionDigits = value < 10 ? 1 : 0;
  return value.toFixed(fractionDigits).replace(/\.0$/, "");
}

function formatPriceAxisValue(value: number): string {
  if (!Number.isFinite(value)) {
    return "-";
  }
  return Math.round(value).toLocaleString("en-US");
}

function hasVolumePane(scene: ChartScene): boolean {
  return scene.chart.layers.volume && scene.plot.volumeTop < scene.plot.bottom;
}

function drawCrosshair(context: CanvasRenderingContext2D, scene: ChartScene, crosshair?: { x: number; y: number }) {
  if (!crosshair || crosshair.x < scene.plot.left || crosshair.x > scene.plot.right || crosshair.y < scene.plot.top || crosshair.y > scene.plot.bottom) {
    return;
  }
  const semanticHit = hitTestSemanticNode(scene, crosshair.x, crosshair.y);
  if (!semanticHit) {
    return;
  }
  const x = unitCenterX(scene, semanticHit);
  const y = Math.max(scene.plot.top, Math.min(scene.plot.priceBottom, crosshair.y));
  const drawingToolActive = scene.chart.toolMode !== "pan" && scene.chart.toolMode !== "select";
  const alpha = drawingToolActive ? 0.12 : 0.22;
  const label = semanticHit.kind === "candle"
    ? formatSemanticTimestamp(semanticHit.timestamp, semanticHit.interval)
    : formatSemanticTimestamp(semanticHit.from, semanticHit.interval);
  const inPricePane = crosshair.y <= scene.plot.priceBottom;
  const inVolumePane = hasVolumePane(scene) && crosshair.y >= scene.plot.volumeTop && crosshair.y <= scene.plot.bottom;
  context.save();
  context.strokeStyle = `rgba(20, 20, 20, ${alpha})`;
  context.lineWidth = 1;
  context.setLineDash([]);
  line(context, x, 0, x, scene.height);
  if (inPricePane) {
    line(context, scene.plot.left, y, horizontalGuideRight(scene), y);
  } else if (inVolumePane) {
    line(context, scene.plot.left, crosshair.y, horizontalGuideRight(scene), crosshair.y);
  }
  drawAxisPill(context, label, x, timeAxisY(scene), "center");
  if (inPricePane) {
    const price = createCoordinateTransform(scene).yToPrice(y);
    drawAxisPill(context, price.toFixed(2), scene.width - 8, y, "right");
  } else if (inVolumePane) {
    drawAxisPill(context, formatVolumeAxisValue(volumeAtY(scene, crosshair.y)), scene.width - 8, crosshair.y, "right");
  }
  context.restore();
}

function drawAxisPill(
  context: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  align: "center" | "left" | "right"
) {
  context.font = "10px Inter, system-ui, sans-serif";
  const metrics = context.measureText(text);
  const width = metrics.width + 10;
  const height = 17;
  const left = align === "right" ? x - width : align === "left" ? x : x - width / 2;
  const top = y - height / 2;
  context.fillStyle = "rgba(247, 250, 252, 0.82)";
  context.strokeStyle = "rgba(129, 145, 168, 0.28)";
  context.lineWidth = 1;
  roundedRect(context, left, top, width, height, 5);
  context.fill();
  context.stroke();
  context.fillStyle = "rgba(38, 54, 76, 0.82)";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText(text, left + width / 2, y + 0.5);
}

function drawEmpty(context: CanvasRenderingContext2D, width: number, height: number, message: string) {
  context.fillStyle = colors.text;
  context.font = "12px Inter, system-ui, sans-serif";
  context.textAlign = "center";
  context.fillText(message, width / 2, height / 2);
  context.textAlign = "start";
}

function line(context: CanvasRenderingContext2D, x1: number, y1: number, x2: number, y2: number) {
  context.beginPath();
  context.moveTo(Math.round(x1) + 0.5, Math.round(y1) + 0.5);
  context.lineTo(Math.round(x2) + 0.5, Math.round(y2) + 0.5);
  context.stroke();
}

function horizontalGuideRight(scene: ChartScene): number {
  return Math.max(scene.plot.right, scene.width - 52);
}

function drawExpansionRanges(context: CanvasRenderingContext2D, scene: ChartScene, active: boolean) {
  scene.semantic.expansionRanges.forEach((range) => {
    const left = Math.max(scene.plot.left, range.left);
    const right = Math.min(scene.plot.right, range.right);
    const width = right - left;
    if (width <= 4) {
      return;
    }
    context.save();
    context.beginPath();
    context.rect(left, 0, width, scene.height);
    context.clip();
    const depthAlpha = Math.min(0.046, 0.018 + range.depth * 0.006);
    context.fillStyle = range.childInterval === "footprint"
      ? `rgba(245, 158, 11, ${Math.min(0.046, depthAlpha + 0.004)})`
      : `rgba(37, 99, 235, ${depthAlpha})`;
    context.fillRect(left, 0, width, scene.height);

    if (active) {
      const sideWidth = Math.min(8, Math.max(4, width / 3));
      if (range.left >= scene.plot.left) {
        const leftGradient = context.createLinearGradient(left, 0, left + sideWidth, 0);
        leftGradient.addColorStop(0, "rgba(22, 32, 51, 0.095)");
        leftGradient.addColorStop(1, "rgba(22, 32, 51, 0)");
        context.fillStyle = leftGradient;
        context.fillRect(left, 0, sideWidth, scene.height);
      }

      if (range.right <= scene.plot.right) {
        const rightGradient = context.createLinearGradient(right - sideWidth, 0, right, 0);
        rightGradient.addColorStop(0, "rgba(22, 32, 51, 0)");
        rightGradient.addColorStop(1, "rgba(22, 32, 51, 0.095)");
        context.fillStyle = rightGradient;
        context.fillRect(right - sideWidth, 0, sideWidth, scene.height);
      }
    }
    context.restore();
  });
  scene.semantic.units.forEach((unit) => {
    if (unit.kind === "placeholder" || unit.kind === "footprint") {
      drawSemanticPlaceholder(context, scene, unit);
    }
  });
}

function drawExpansionParentSummaries(context: CanvasRenderingContext2D, scene: ChartScene) {
  scene.semantic.expansionRanges.forEach((range) => {
    const { left, right } = expansionSummaryVisibleBounds(scene.plot, range);
    const availableWidth = right - left;
    if (availableWidth < 16) {
      return;
    }
    const candle = range.parentCandle;
    const up = candle.close >= candle.open;
    const candleHeight = expansionParentCandleHeight;
    const candleTop = expansionMetadataTop(scene.plot.top);
    const priceRange = Math.max(0.0001, candle.high - candle.low);
    const localY = (price: number) => candleTop + ((candle.high - price) / priceRange) * candleHeight;
    const open = localY(candle.open);
    const close = localY(candle.close);
    const high = localY(candle.high);
    const low = localY(candle.low);
    const bodyTop = Math.min(open, close);
    const bodyHeight = Math.max(3, Math.abs(close - open));
    const bodyBottom = bodyTop + bodyHeight;
    const candleWidth = expansionParentCandleWidth;
    const candleCenter = Math.round(Math.min(left + 9, left + availableWidth / 2)) + 0.5;
    const bodyLeft = candleCenter - candleWidth / 2;

    context.save();
    context.strokeStyle = up ? colors.bullish : colors.bearish;
    context.fillStyle = up ? colors.bullish : colors.bearish;
    context.globalAlpha = 0.72;
    context.lineWidth = 1;
    line(context, candleCenter, high, candleCenter, bodyTop);
    line(context, candleCenter, bodyBottom, candleCenter, low);
    context.fillRect(bodyLeft, bodyTop, candleWidth, bodyHeight);
    context.globalAlpha = 1;
    context.fillStyle = "rgba(38, 54, 76, 0.76)";
    context.font = "10px Inter, system-ui, sans-serif";
    context.textAlign = "left";
    context.textBaseline = "middle";
    const textLeft = candleCenter + 12;
    const textWidth = right - textLeft;
    const summaryColumns = buildParentSummaryColumns(range, textWidth);
    if (summaryColumns.length) {
      const columnGap = 54;
      summaryColumns.forEach((column, index) => {
        const columnX = textLeft + index * columnGap;
        context.fillText(column.top, columnX, candleTop + candleHeight / 2 - 6);
        context.fillText(column.bottom, columnX, candleTop + candleHeight / 2 + 6);
      });
    }
    context.restore();
  });
}

function buildParentSummaryColumns(
  range: ChartScene["semantic"]["expansionRanges"][number],
  width: number
): Array<{ top: string; bottom: string }> {
  if (width < 72) {
    return [];
  }
  const candle = range.parentCandle;
  const columns = [
    {
      top: formatParentSummaryDate(range.from),
      bottom: formatParentSummaryDate(range.to)
    }
  ];
  if (width >= 132) {
    columns.push({
      top: `O ${candle.open.toFixed(2)}`,
      bottom: `C ${candle.close.toFixed(2)}`
    });
  }
  if (width >= 190) {
    columns.push({
      top: `H ${candle.high.toFixed(2)}`,
      bottom: `L ${candle.low.toFixed(2)}`
    });
  }
  return columns;
}

function formatParentSummaryDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "2-digit"
  }).format(date);
}

function drawSemanticPlaceholder(context: CanvasRenderingContext2D, scene: ChartScene, unit: Extract<SemanticRenderUnit, { kind: "placeholder" | "footprint" }>) {
  const bounds = unitBoundsX(scene, unit);
  const visibleLeft = Math.max(scene.plot.left, bounds.left);
  const visibleRight = Math.min(scene.plot.right, bounds.right);
  const visibleWidth = visibleRight - visibleLeft;
  if (visibleWidth <= 4) {
    return;
  }
  const x = (visibleLeft + visibleRight) / 2;
  const y = scene.plot.top + (scene.plot.priceBottom - scene.plot.top) / 2;
  context.save();
  context.fillStyle = unit.kind === "footprint" ? "#98650d" : colors.muted;
  context.font = "10px Inter, system-ui, sans-serif";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText(unit.message, x, y, Math.max(24, visibleWidth - 8));
  context.restore();
}

function candleUnits(scene: ChartScene): SemanticCandleUnit[] {
  return scene.semantic.units.filter((unit): unit is SemanticCandleUnit => unit.kind === "candle");
}

function circle(context: CanvasRenderingContext2D, x: number, y: number, radius: number) {
  context.beginPath();
  context.arc(x, y, radius, 0, Math.PI * 2);
}

function drawArrowHead(context: CanvasRenderingContext2D, from: { x: number; y: number }, to: { x: number; y: number }) {
  const angle = Math.atan2(to.y - from.y, to.x - from.x);
  const length = 9;
  context.beginPath();
  context.moveTo(to.x, to.y);
  context.lineTo(to.x - length * Math.cos(angle - Math.PI / 6), to.y - length * Math.sin(angle - Math.PI / 6));
  context.moveTo(to.x, to.y);
  context.lineTo(to.x - length * Math.cos(angle + Math.PI / 6), to.y - length * Math.sin(angle + Math.PI / 6));
  context.stroke();
}

function roundedRect(context: CanvasRenderingContext2D, x: number, y: number, width: number, height: number, radius: number) {
  const safeRadius = Math.min(radius, width / 2, height / 2);
  context.beginPath();
  context.moveTo(x + safeRadius, y);
  context.arcTo(x + width, y, x + width, y + height, safeRadius);
  context.arcTo(x + width, y + height, x, y + height, safeRadius);
  context.arcTo(x, y + height, x, y, safeRadius);
  context.arcTo(x, y, x + width, y, safeRadius);
  context.closePath();
}
