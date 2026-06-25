import type { HitTestResult } from "./hitTest";
import type { BuildRenderLayerArgs, RenderLayer, RenderPane, RenderScale } from "./sceneBuilder";
import type { LayerType } from "../types/documents";
import type { IndicatorPoint } from "../types/calculations";
import { paddedDomain } from "./scales";
import { createValueScaleModel } from "./valueScaleModel";

export interface CanvasPoint {
  x: number;
  y: number;
}

export interface LayerRendererDefinition {
  type: LayerType;
  buildRenderLayer(args: BuildRenderLayerArgs): RenderLayer | null;
  draw(ctx: CanvasRenderingContext2D, layer: RenderLayer, pane: RenderPane): void;
  hitTest?(point: CanvasPoint, layer: RenderLayer, pane: RenderPane): HitTestResult | null;
}

export type LayerRendererRegistry = Partial<Record<LayerType, LayerRendererDefinition>>;

export const ENABLED_LAYER_TYPES: LayerType[] = [
  "priceSeries",
  "volume",
  "indicator",
  "comparisonSeries",
  "drawing",
  "aiProposal"
];

export const defaultLayerRendererRegistry: LayerRendererRegistry = {
  priceSeries: {
    type: "priceSeries",
    buildRenderLayer({ layer, visibleCandles }) {
      if (layer.type !== "priceSeries") return null;
      return {
        id: layer.id,
        type: "priceSeries",
        paneId: layer.paneId,
        zIndex: layer.zIndex,
        visible: layer.visible,
        style: layer.style,
        seriesType: layer.seriesType,
        candles: visibleCandles
      };
    },
    draw(ctx, layer, pane) {
      if (layer.type !== "priceSeries") return;
      drawCandles(ctx, layer.candles, pane);
    }
  },
  volume: {
    type: "volume",
    buildRenderLayer({ layer, visibleCandles }) {
      if (layer.type !== "volume") return null;
      return {
        id: layer.id,
        type: "volume",
        paneId: layer.paneId,
        zIndex: layer.zIndex,
        visible: layer.visible,
        style: layer.style,
        candles: visibleCandles
      };
    },
    draw(ctx, layer, pane) {
      if (layer.type !== "volume") return;
      drawVolume(ctx, layer.candles, pane);
    }
  },
  indicator: {
    type: "indicator",
    buildRenderLayer({ layer, calculationOutputs, visibleCandles }) {
      if (layer.type !== "indicator") return null;
      const output = calculationOutputs[layer.calculationNodeId];
      if (!output) return null;
      const visibleTimestamps = new Set(visibleCandles.map((candle) => candle.timestamp));
      return {
        id: layer.id,
        type: "indicator",
        paneId: layer.paneId,
        zIndex: layer.zIndex,
        visible: layer.visible,
        style: layer.style,
        series: output.series.map((series) => ({
          ...series,
          points: series.points.filter((point) => visibleTimestamps.has(point.timestamp))
        }))
      };
    },
    draw(ctx, layer, pane) {
      if (layer.type !== "indicator") return;
      layer.series.forEach((series, index) => {
        const color = layer.style.color ?? indicatorColor(index);
        if (series.renderMode === "band") drawBand(ctx, series.points, pane, layer.style.fill ?? "rgba(245, 158, 11, 0.08)", color);
        else drawLine(ctx, series.points, pane, color, layer.style.lineWidth ?? 1.8, layer.style.lineDash, layer.style.opacity);
      });
    }
  },
  comparisonSeries: {
    type: "comparisonSeries",
    buildRenderLayer({ layer, candlesBySymbol, visibleCandles }) {
      if (layer.type !== "comparisonSeries") return null;
      const comparisonCandles = candlesBySymbol[layer.symbol] ?? [];
      const comparisonByTimestamp = new Map(comparisonCandles.map((candle) => [candle.timestamp, candle]));
      const visibleComparisonCandles = visibleCandles.map((candle) => comparisonByTimestamp.get(candle.timestamp) ?? null);
      const previousClose = previousComparisonClose(comparisonCandles, visibleCandles[0]?.timestamp);
      const baseIndex = Math.max(0, visibleCandles.findIndex((candle) => candle.finalized));
      const baseTimestamp = visibleCandles[baseIndex]?.timestamp;
      let firstVisibleClose: number | null = baseTimestamp ? comparisonByTimestamp.get(baseTimestamp)?.close ?? null : null;
      let baselineUsed: number | null = layer.baselineMode === "previousClose" ? previousClose : firstVisibleClose;
      const points = visibleComparisonCandles.map((candle, index) => {
        if (!candle) return { timestamp: visibleCandles[index].timestamp, value: null };
        if (firstVisibleClose === null) firstVisibleClose = candle.close;
        if (baselineUsed === null) baselineUsed = layer.baselineMode === "previousClose" ? previousClose ?? firstVisibleClose : firstVisibleClose;
        const baseline = layer.baselineMode === "previousClose" ? previousClose ?? firstVisibleClose : firstVisibleClose;
        return {
          timestamp: candle.timestamp,
          value: baseline ? ((candle.close - baseline) / baseline) * 100 : 0
        };
      });
      return {
        id: layer.id,
        type: "comparisonSeries",
        paneId: layer.paneId,
        zIndex: layer.zIndex,
        visible: layer.visible,
        style: layer.style,
        symbol: layer.symbol,
        points,
        baseTimestamp,
        baseClose: baselineUsed ?? undefined,
        baselineMode: layer.baselineMode
      };
    },
    draw(ctx, layer, pane) {
      if (layer.type !== "comparisonSeries") return;
      drawComparisonLine(ctx, layer.points, pane, layer.style.color ?? "#a78bfa", layer.style.lineWidth ?? 1.5, layer.style.lineDash, layer.style.opacity);
    }
  },
  drawing: {
    type: "drawing",
    buildRenderLayer({ layer }) {
      if (layer.type !== "drawing") return null;
      return {
        id: layer.id,
        type: "drawing",
        paneId: layer.paneId,
        zIndex: layer.zIndex,
        visible: layer.visible,
        style: layer.style,
        drawing: layer.drawing
      };
    },
    draw(ctx, layer, pane) {
      if (layer.type !== "drawing") return;
      drawDrawing(ctx, layer, pane);
    },
    hitTest(point, layer, pane) {
      if (layer.type !== "drawing" || layer.drawing.kind !== "horizontalLine") return null;
      const y = pane.yScale.valueToY(layer.drawing.price);
      const withinX = point.x >= pane.bounds.x && point.x <= pane.bounds.x + pane.bounds.width;
      if (!withinX || Math.abs(point.y - y) > 7) return null;
      return {
        paneId: pane.id,
        layerId: layer.id,
        price: layer.drawing.price,
        drawingHandle: "body"
      };
    }
  },
  aiProposal: {
    type: "aiProposal",
    buildRenderLayer({ layer }) {
      if (layer.type !== "aiProposal") return null;
      return {
        id: layer.id,
        type: "aiProposal",
        paneId: layer.paneId,
        zIndex: layer.zIndex,
        visible: layer.visible,
        style: layer.style,
        proposalId: layer.proposalId,
        previewLayers: []
      };
    },
    draw() {}
  }
};

function drawCandles(ctx: CanvasRenderingContext2D, candles: import("../types/market").Candle[], pane: RenderPane): void {
  if (candles.length === 0) return;
  const bodyWidth = pane.xScale.candleBodyWidth;
  candles.forEach((candle, index) => {
    const x = pane.xScale.timestampToX(candle.timestamp) ?? pane.xScale.logicalToX(pane.xScale.visibleDataRange.from + index);
    const openY = pane.yScale.valueToY(candle.open);
    const closeY = pane.yScale.valueToY(candle.close);
    const highY = pane.yScale.valueToY(candle.high);
    const lowY = pane.yScale.valueToY(candle.low);
    const up = candle.close >= candle.open;
    ctx.strokeStyle = up ? "#22c55e" : "#ef4444";
    ctx.fillStyle = up ? "rgba(34, 197, 94, 0.82)" : "rgba(239, 68, 68, 0.82)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, highY);
    ctx.lineTo(x, lowY);
    ctx.stroke();
    const top = Math.min(openY, closeY);
    const height = Math.max(1, Math.abs(closeY - openY));
    ctx.fillRect(x - bodyWidth / 2, top, bodyWidth, height);
  });
}

function drawVolume(ctx: CanvasRenderingContext2D, candles: import("../types/market").Candle[], pane: RenderPane): void {
  if (candles.length === 0) return;
  const width = Math.max(1, Math.min(12, pane.xScale.barSpacing * 0.62));
  candles.forEach((candle, index) => {
    const center = pane.xScale.timestampToX(candle.timestamp) ?? pane.xScale.logicalToX(pane.xScale.visibleDataRange.from + index);
    const x = center - width / 2;
    const y = pane.yScale.valueToY(candle.volume);
    const base = pane.yScale.valueToY(0);
    ctx.fillStyle = candle.close >= candle.open ? "rgba(34, 197, 94, 0.34)" : "rgba(239, 68, 68, 0.34)";
    ctx.fillRect(x, y, width, Math.max(1, base - y));
  });
}

function drawLine(
  ctx: CanvasRenderingContext2D,
  points: IndicatorPoint[],
  pane: RenderPane,
  color: string,
  width: number,
  lineDash?: number[],
  opacity = 1,
  yScale: RenderScale = pane.yScale
): void {
  const valid = points.filter((point) => typeof point.value === "number" && Number.isFinite(point.value));
  if (valid.length === 0) return;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.globalAlpha = opacity;
  if (lineDash) ctx.setLineDash(lineDash);
  ctx.beginPath();
  let started = false;
  points.forEach((point, index) => {
    if (typeof point.value !== "number" || !Number.isFinite(point.value)) {
      started = false;
      return;
    }
    const x = pane.xScale.timestampToX(point.timestamp) ?? pane.xScale.logicalToX(pane.xScale.visibleDataRange.from + index);
    const y = yScale.valueToY(point.value);
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();
  ctx.restore();
}

function drawComparisonLine(
  ctx: CanvasRenderingContext2D,
  points: IndicatorPoint[],
  pane: RenderPane,
  color: string,
  width: number,
  lineDash?: number[],
  opacity?: number
): void {
  const values = points.flatMap((point) =>
    typeof point.value === "number" && Number.isFinite(point.value) ? [point.value] : []
  );
  const yScale: RenderScale =
    pane.comparisonScale ??
    createValueScaleModel({
      scaleId: `${pane.id}-comparison-fallback`,
      mode: "percent",
      domain: paddedDomain(values, [-1, 1]),
      bounds: pane.bounds
    });
  drawLine(ctx, points, pane, color, width, lineDash, opacity, yScale);
}

function previousComparisonClose(candles: import("../types/market").Candle[], firstVisibleTimestamp?: string): number | null {
  if (!firstVisibleTimestamp) return null;
  let previous: number | null = null;
  for (const candle of candles) {
    if (candle.timestamp >= firstVisibleTimestamp) break;
    previous = candle.close;
  }
  return previous;
}

function drawBand(ctx: CanvasRenderingContext2D, points: IndicatorPoint[], pane: RenderPane, fill: string, color: string): void {
  const upper: Array<[number, number]> = [];
  const lower: Array<[number, number]> = [];
  points.forEach((point, index) => {
    const upperValue = point.values?.upper;
    const lowerValue = point.values?.lower;
    if (typeof upperValue === "number" && typeof lowerValue === "number") {
      const x = pane.xScale.timestampToX(point.timestamp) ?? pane.xScale.logicalToX(pane.xScale.visibleDataRange.from + index);
      upper.push([x, pane.yScale.valueToY(upperValue)]);
      lower.push([x, pane.yScale.valueToY(lowerValue)]);
    }
  });
  if (upper.length === 0) return;
  ctx.fillStyle = fill;
  ctx.beginPath();
  upper.forEach(([x, y], index) => (index === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)));
  [...lower].reverse().forEach(([x, y]) => ctx.lineTo(x, y));
  ctx.closePath();
  ctx.fill();
  drawLine(ctx, points.map((point) => ({ ...point, value: point.values?.middle ?? point.value })), pane, color, 1.4);
}

function drawDrawing(ctx: CanvasRenderingContext2D, layer: Extract<RenderLayer, { type: "drawing" }>, pane: RenderPane): void {
  ctx.save();
  ctx.strokeStyle = layer.style.color ?? "#38bdf8";
  ctx.fillStyle = layer.style.textColor ?? layer.style.color ?? "#38bdf8";
  ctx.lineWidth = layer.style.lineWidth ?? 1.5;
  ctx.globalAlpha = layer.style.opacity ?? 1;
  if (layer.style.lineDash) ctx.setLineDash(layer.style.lineDash);
  if (layer.drawing.kind === "horizontalLine") {
    const y = pane.yScale.valueToY(layer.drawing.price);
    if (layer.selected) {
      ctx.lineWidth = (layer.style.lineWidth ?? 1.5) + 1.5;
      ctx.shadowColor = layer.style.color ?? "#38bdf8";
      ctx.shadowBlur = 8;
    }
    ctx.beginPath();
    ctx.moveTo(pane.bounds.x, y);
    ctx.lineTo(pane.bounds.x + pane.bounds.width, y);
    ctx.stroke();
    ctx.shadowBlur = 0;
    if (layer.selected) {
      ctx.fillStyle = layer.style.color ?? "#38bdf8";
      ctx.fillRect(pane.bounds.x + 6, y - 3, 6, 6);
      ctx.fillRect(pane.bounds.x + pane.bounds.width - 12, y - 3, 6, 6);
    }
    if (layer.drawing.label) {
      ctx.font = "12px Inter, system-ui, sans-serif";
      ctx.fillText(layer.drawing.label, pane.bounds.x + 8, y - 6);
    }
  }
  ctx.restore();
}

function indicatorColor(index: number): string {
  return ["#f59e0b", "#38bdf8", "#a78bfa", "#f472b6"][index % 4];
}
