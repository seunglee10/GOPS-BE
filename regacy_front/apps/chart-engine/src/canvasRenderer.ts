import { createCoordinateTransform } from "./scales";
import { normalizeLineExtension, projectTrendLine } from "./drawingGeometry";
import { buildTimeAxisLayout, formatCrosshairTimestamp } from "./time";
import type { DrawingEntity, RenderScene } from "./types";

export function drawChartScene(
  canvas: HTMLCanvasElement,
  scene: RenderScene,
  dpr = typeof window === "undefined" ? 1 : window.devicePixelRatio || 1
) {
  const ctx = canvas.getContext("2d");
  if (!ctx) {
    return;
  }

  canvas.width = Math.max(1, Math.floor(scene.width * dpr));
  canvas.height = Math.max(1, Math.floor(scene.height * dpr));
  canvas.style.width = `${scene.width}px`;
  canvas.style.height = `${scene.height}px`;

  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, scene.width, scene.height);
  ctx.fillStyle = scene.document.style.background;
  ctx.fillRect(0, 0, scene.width, scene.height);

  if (scene.state !== "ready") {
    drawState(ctx, scene);
    return;
  }

  drawGrid(ctx, scene);
  drawVolume(ctx, scene);
  drawCandles(ctx, scene);
  drawComparisons(ctx, scene);
  drawMovingAverage(ctx, scene, "ma60", scene.document.style.ma60);
  drawMovingAverage(ctx, scene, "ma20", scene.document.style.ma20);
  drawMovingAverage(ctx, scene, "ma5", scene.document.style.ma5);
  drawDrawings(ctx, scene, scene.document.drawings, false);
  if (scene.pendingPreview?.visible) {
    drawDrawings(ctx, scene, scene.pendingPreview.drawings, true);
    drawPreviewComparisons(ctx, scene);
  }
  drawCrosshair(ctx, scene);
  drawAxes(ctx, scene);
}

function drawState(ctx: CanvasRenderingContext2D, scene: RenderScene) {
  ctx.fillStyle = scene.document.style.text;
  ctx.font = "12px Inter, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const lines = wrapCanvasText(ctx, scene.message ?? scene.state, Math.max(120, scene.width - 72));
  const lineHeight = 18;
  const startY = scene.height / 2 - ((lines.length - 1) * lineHeight) / 2;
  lines.forEach((line, index) => {
    ctx.fillText(line, scene.width / 2, startY + index * lineHeight);
  });
}

function wrapCanvasText(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string[] {
  const words = text.split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let current = "";
  words.forEach((word) => {
    const candidate = current ? `${current} ${word}` : word;
    if (ctx.measureText(candidate).width <= maxWidth) {
      current = candidate;
      return;
    }
    if (current) {
      lines.push(current);
      current = word;
      return;
    }
    lines.push(word);
  });
  if (current) {
    lines.push(current);
  }
  return lines.length ? lines : [text];
}

function drawGrid(ctx: CanvasRenderingContext2D, scene: RenderScene) {
  const { left, right, top, priceBottom, volumeTop, bottom } = scene.plot;
  ctx.strokeStyle = scene.document.style.grid;
  ctx.lineWidth = 1;

  for (let index = 0; index <= 4; index += 1) {
    const y = top + ((priceBottom - top) * index) / 4;
    line(ctx, left, y, right, y);
  }

  for (let index = 0; index <= 5; index += 1) {
    const x = left + ((right - left) * index) / 5;
    line(ctx, x, top, x, bottom);
  }

  drawDateBoundaries(ctx, scene);
  line(ctx, left, volumeTop, right, volumeTop);
}

function drawDateBoundaries(ctx: CanvasRenderingContext2D, scene: RenderScene) {
  const axis = buildTimeAxisLayout(scene.candles, scene.document.timeframe, scene.plot.right - scene.plot.left, {
    visibleSlotCount: scene.visibleSlotCount
  });
  if (axis.boundaries.length === 0) {
    return;
  }

  ctx.save();
  ctx.strokeStyle = "rgba(100, 116, 139, 0.28)";
  ctx.lineWidth = 1;
  axis.boundaries.forEach((boundary) => {
    const x = candleCenter(scene, boundary.visibleIndex);
    line(ctx, x, scene.plot.top, x, scene.plot.bottom);
  });
  ctx.restore();
}

function drawCandles(ctx: CanvasRenderingContext2D, scene: RenderScene) {
  if (!scene.document.layers.candles) {
    return;
  }

  scene.candles.forEach((candle, index) => {
    const x = candleCenter(scene, index);
    const openY = priceY(scene, candle.open);
    const closeY = priceY(scene, candle.close);
    const highY = priceY(scene, candle.high);
    const lowY = priceY(scene, candle.low);
    const isUp = candle.close >= candle.open;
    const color = isUp ? scene.document.style.bullish : scene.document.style.bearish;
    const bodyTop = Math.min(openY, closeY);
    const bodyHeight = Math.max(1.5, Math.abs(closeY - openY));

    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 1;
    line(ctx, x, highY, x, lowY);
    ctx.fillRect(x - scene.scales.candleWidth / 2, bodyTop, scene.scales.candleWidth, bodyHeight);
  });
}

function drawVolume(ctx: CanvasRenderingContext2D, scene: RenderScene) {
  if (!scene.document.layers.volume) {
    return;
  }

  const volumeHeight = scene.plot.bottom - scene.plot.volumeTop;
  scene.candles.forEach((candle, index) => {
    const x = candleCenter(scene, index);
    const height = Math.max(1, (candle.volume / scene.scales.maxVolume) * volumeHeight);
    const y = scene.plot.bottom - height;
    const color = candle.close >= candle.open ? "rgba(22, 168, 107, 0.26)" : "rgba(233, 75, 91, 0.24)";

    ctx.fillStyle = color;
    ctx.fillRect(x - scene.scales.candleWidth / 2, y, scene.scales.candleWidth, height);
  });
}

function drawMovingAverage(ctx: CanvasRenderingContext2D, scene: RenderScene, key: "ma5" | "ma20" | "ma60", color: string) {
  if (!scene.document.layers[key]) {
    return;
  }

  ctx.strokeStyle = color;
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  let started = false;

  scene.candles.forEach((candle, index) => {
    const value = candle[key];
    if (typeof value !== "number") {
      return;
    }
    const x = candleCenter(scene, index);
    const y = priceY(scene, value);
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else {
      ctx.lineTo(x, y);
    }
  });

  if (started) {
    ctx.stroke();
  }
}

function drawComparisons(ctx: CanvasRenderingContext2D, scene: RenderScene) {
  scene.comparisonSeries.forEach((series) => {
    if (series.points.length < 2) {
      return;
    }
    ctx.save();
    ctx.globalAlpha = series.comparison.style.opacity ?? 1;
    ctx.strokeStyle = series.comparison.style.color ?? "#111111";
    ctx.lineWidth = series.comparison.style.lineWidth ?? 1.4;
    ctx.setLineDash(series.comparison.style.lineDash ?? []);
    ctx.beginPath();
    series.points.forEach((point, index) => {
      if (index === 0) {
        ctx.moveTo(point.x, point.y);
      } else {
        ctx.lineTo(point.x, point.y);
      }
    });
    ctx.stroke();
    const last = series.points[series.points.length - 1];
    if (last) {
      ctx.fillStyle = series.comparison.style.textColor ?? series.comparison.style.color ?? "#111111";
      ctx.font = "11px Inter, system-ui, sans-serif";
      ctx.textAlign = "right";
      ctx.fillText(`${series.comparison.label ?? series.comparison.symbol} ${last.percent >= 0 ? "+" : ""}${last.percent.toFixed(2)}%`, scene.plot.right, last.y - 8);
    }
    ctx.restore();
  });
}

function drawPreviewComparisons(ctx: CanvasRenderingContext2D, scene: RenderScene) {
  const previewComparisons = scene.pendingPreview?.comparisons ?? [];
  if (previewComparisons.length === 0) {
    return;
  }
  ctx.save();
  ctx.fillStyle = "rgba(17, 17, 17, 0.74)";
  ctx.font = "11px Inter, system-ui, sans-serif";
  ctx.textAlign = "left";
  previewComparisons.forEach((comparison, index) => {
    ctx.fillText(`비교 미리보기: ${comparison.label ?? comparison.symbol}`, scene.plot.left, scene.plot.top + 16 + index * 15);
  });
  ctx.restore();
}

function drawDrawings(ctx: CanvasRenderingContext2D, scene: RenderScene, drawings: DrawingEntity[], preview: boolean) {
  const transform = createCoordinateTransform(scene);
  drawings.filter((drawing) => drawing.visible !== false).forEach((drawing) => {
    const selected = !preview && scene.document.selectedDrawingId === drawing.id;
    const style = drawing.style ?? {};
    ctx.save();
    ctx.globalAlpha = preview ? 0.58 : style.opacity ?? 1;
    ctx.strokeStyle = style.color ?? (preview ? "#2563eb" : "#111111");
    ctx.fillStyle = style.fillColor ?? (preview ? "rgba(37, 99, 235, 0.08)" : "rgba(17, 17, 17, 0.08)");
    ctx.lineWidth = selected ? Math.max(2.2, style.lineWidth ?? 1.5) : style.lineWidth ?? 1.5;
    ctx.setLineDash(preview ? [6, 4] : style.lineDash ?? []);
    const points = drawing.anchors.map((anchor) => transform.anchorToPoint(anchor)).filter((point): point is { x: number; y: number } => Boolean(point));

    if (drawing.type === "horizontalLine" && points[0]) {
      line(ctx, scene.plot.left, points[0].y, scene.plot.right, points[0].y);
      drawDrawingLabel(ctx, drawing.label, scene.plot.right - 4, points[0].y - 8, drawing);
    } else if (drawing.type === "verticalMarker" && points[0]) {
      line(ctx, points[0].x, scene.plot.top, points[0].x, scene.plot.priceBottom);
      drawDrawingLabel(ctx, drawing.label, points[0].x + 5, scene.plot.top + 12, drawing);
    } else if ((drawing.type === "trendLine" || drawing.type === "arrow" || drawing.type === "measurement") && points.length >= 2) {
      const [start, end] = drawing.type === "trendLine"
        ? projectTrendLine(points[0], points[1], scene.plot, normalizeLineExtension(style.extension))
        : [points[0], points[1]];
      line(ctx, start.x, start.y, end.x, end.y);
      if (drawing.type === "arrow") {
        drawArrowHead(ctx, start, end);
      }
      drawDrawingLabel(ctx, drawing.label ?? measurementLabel(drawing, scene), (start.x + end.x) / 2, (start.y + end.y) / 2 - 8, drawing);
    } else if (drawing.type === "rangeBox" && points.length >= 2) {
      const x = Math.min(points[0].x, points[1].x);
      const y = Math.min(points[0].y, points[1].y);
      const width = Math.abs(points[1].x - points[0].x);
      const height = Math.abs(points[1].y - points[0].y);
      ctx.fillRect(x, y, width, height);
      ctx.strokeRect(x, y, width, height);
      drawDrawingLabel(ctx, drawing.label, x + 5, y + 13, drawing);
    } else if ((drawing.type === "pointMarker" || drawing.type === "textLabel") && points[0]) {
      circle(ctx, points[0].x, points[0].y, drawing.type === "pointMarker" ? 4 : 3);
      ctx.fill();
      drawDrawingLabel(ctx, drawing.label ?? (drawing.type === "textLabel" ? "메모" : ""), points[0].x + 7, points[0].y - 7, drawing);
    }

    if (selected && points.length > 0) {
      ctx.setLineDash([]);
      ctx.fillStyle = "#ffffff";
      ctx.strokeStyle = "#111111";
      points.forEach((point) => {
        circle(ctx, point.x, point.y, 4);
        ctx.fill();
        ctx.stroke();
      });
    }
    ctx.restore();
  });
}

function drawDrawingLabel(ctx: CanvasRenderingContext2D, label: string | undefined, x: number, y: number, drawing: DrawingEntity) {
  if (!label) {
    return;
  }
  const style = drawing.style ?? {};
  ctx.fillStyle = style.textColor ?? style.color ?? "#111111";
  ctx.font = `${style.fontSize ?? 12}px Inter, system-ui, sans-serif`;
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(label, x, y);
}

function measurementLabel(drawing: DrawingEntity, scene: RenderScene): string {
  const [start, end] = drawing.anchors;
  if (typeof start?.price !== "number" || typeof end?.price !== "number") {
    return drawing.label ?? "측정";
  }
  const delta = end.price - start.price;
  const percent = (delta / Math.max(0.0001, start.price)) * 100;
  const startIndex = scene.allCandles.findIndex((candle) => candle.timestamp === start.timestamp);
  const endIndex = scene.allCandles.findIndex((candle) => candle.timestamp === end.timestamp);
  const bars = startIndex >= 0 && endIndex >= 0 ? Math.abs(endIndex - startIndex) : 0;
  return `${delta >= 0 ? "+" : ""}${delta.toFixed(2)} / ${percent >= 0 ? "+" : ""}${percent.toFixed(2)}% / ${bars}봉`;
}

function drawAxes(ctx: CanvasRenderingContext2D, scene: RenderScene) {
  const { left, right, top, priceBottom, bottom } = scene.plot;
  const priceLabelX = scene.width - 6;
  ctx.fillStyle = scene.document.style.text;
  ctx.font = "11px Inter, system-ui, sans-serif";
  ctx.textBaseline = "middle";

  ctx.textAlign = "right";
  for (let index = 0; index <= 4; index += 1) {
    const ratio = index / 4;
    const value = scene.scales.maxPrice - (scene.scales.maxPrice - scene.scales.minPrice) * ratio;
    const y = top + (priceBottom - top) * ratio;
    ctx.fillText(value.toFixed(2), priceLabelX, y);
  }

  const axis = buildTimeAxisLayout(scene.candles, scene.document.timeframe, right - left, {
    visibleSlotCount: scene.visibleSlotCount
  });
  axis.labels.forEach((label) => {
    const x = candleCenter(scene, label.visibleIndex);
    ctx.textAlign = x - left < 28 ? "left" : right - x < 28 ? "right" : "center";
    ctx.fillText(label.label, x, bottom + 12);
  });
}

function drawCrosshair(ctx: CanvasRenderingContext2D, scene: RenderScene) {
  const crosshair = scene.crosshair;
  if (!crosshair) {
    return;
  }

  ctx.save();
  ctx.strokeStyle = "rgba(20, 20, 20, 0.42)";
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  line(ctx, crosshair.x, scene.plot.top, crosshair.x, scene.plot.bottom);
  line(ctx, scene.plot.left, crosshair.y, scene.plot.right, crosshair.y);
  ctx.setLineDash([]);

  const candle = crosshair.candle;
  const isUp = candle.close >= candle.open;
  const text = `${formatCrosshairTimestamp(candle.timestamp, scene.document.timeframe)} O ${candle.open.toFixed(2)} H ${candle.high.toFixed(2)} L ${candle.low.toFixed(2)} C ${candle.close.toFixed(2)}`;
  ctx.font = "11px Inter, system-ui, sans-serif";
  const textWidth = Math.min(scene.plot.right - scene.plot.left - 12, ctx.measureText(text).width + 12);
  const boxX = Math.min(scene.plot.right - textWidth, Math.max(scene.plot.left, crosshair.x + 8));
  const boxY = scene.plot.top + 8;
  ctx.fillStyle = "rgba(255, 255, 255, 0.92)";
  ctx.strokeStyle = "rgba(17, 17, 17, 0.72)";
  roundedRect(ctx, boxX, boxY, textWidth, 25, 5);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = isUp ? scene.document.style.bullish : scene.document.style.bearish;
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(text, boxX + 6, boxY + 13, textWidth - 12);
  ctx.restore();
}

function priceY(scene: RenderScene, value: number): number {
  const range = Math.max(0.0001, scene.scales.maxPrice - scene.scales.minPrice);
  const ratio = (scene.scales.maxPrice - value) / range;
  return scene.plot.top + ratio * (scene.plot.priceBottom - scene.plot.top);
}

function candleCenter(scene: RenderScene, index: number): number {
  const logicalIndex = scene.visibleStartIndex + index;
  return scene.plot.left + scene.scales.slotWidth * (logicalIndex - scene.viewportStartIndex) + scene.scales.slotWidth / 2;
}

function line(ctx: CanvasRenderingContext2D, x1: number, y1: number, x2: number, y2: number) {
  ctx.beginPath();
  ctx.moveTo(Math.round(x1) + 0.5, Math.round(y1) + 0.5);
  ctx.lineTo(Math.round(x2) + 0.5, Math.round(y2) + 0.5);
  ctx.stroke();
}

function circle(ctx: CanvasRenderingContext2D, x: number, y: number, radius: number) {
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
}

function drawArrowHead(ctx: CanvasRenderingContext2D, from: { x: number; y: number }, to: { x: number; y: number }) {
  const angle = Math.atan2(to.y - from.y, to.x - from.x);
  const length = 9;
  ctx.beginPath();
  ctx.moveTo(to.x, to.y);
  ctx.lineTo(to.x - length * Math.cos(angle - Math.PI / 6), to.y - length * Math.sin(angle - Math.PI / 6));
  ctx.moveTo(to.x, to.y);
  ctx.lineTo(to.x - length * Math.cos(angle + Math.PI / 6), to.y - length * Math.sin(angle + Math.PI / 6));
  ctx.stroke();
}

function roundedRect(ctx: CanvasRenderingContext2D, x: number, y: number, width: number, height: number, radius: number) {
  const safeRadius = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + safeRadius, y);
  ctx.arcTo(x + width, y, x + width, y + height, safeRadius);
  ctx.arcTo(x + width, y + height, x, y + height, safeRadius);
  ctx.arcTo(x, y + height, x, y, safeRadius);
  ctx.arcTo(x, y, x + width, y, safeRadius);
  ctx.closePath();
}
