import type { CandleDto, ChartState, DrawingAnchor } from "./types";
import { normalizeViewport } from "./viewport";
import {
  buildSemanticTimeline,
  type SemanticExpansion,
  type SemanticExpansionRange,
  type SemanticRenderUnit,
  type SemanticTimeline
} from "./semanticTimeline";

const priceAxisWidth = 62;
const volumeScalePadding = 1.18;

export type ChartPlot = {
  left: number;
  right: number;
  top: number;
  bottom: number;
  priceBottom: number;
  volumeTop: number;
};

export type ChartScene = {
  width: number;
  height: number;
  chart: ChartState;
  allCandles: CandleDto[];
  candles: CandleDto[];
  visibleStartIndex: number;
  visibleEndIndex: number;
  viewportStartIndex: number;
  viewportEndIndex: number;
  visibleSlotCount: number;
  semantic: Omit<SemanticTimeline, "expansionRanges"> & { expansionRanges: SemanticExpansionRange[] };
  hoveredNodeId?: string;
  selectedNodeId?: string;
  plot: ChartPlot;
  scales: {
    minPrice: number;
    maxPrice: number;
    priceTicks: number[];
    maxVolume: number;
    volumeTicks: number[];
    slotWidth: number;
    candleWidth: number;
  };
};

export type CoordinateTransform = {
  anchorToPoint: (anchor: DrawingAnchor) => { x: number; y: number } | null;
  pointToAnchor: (x: number, y: number, symbol: string) => DrawingAnchor | null;
  priceToY: (price: number) => number;
  yToPrice: (y: number) => number;
  logicalToX: (logicalIndex: number) => number;
  xToLogical: (x: number) => number;
  timestampToX: (timestamp: string) => number | null;
};

export type ChartSceneOptions = {
  expansions?: SemanticExpansion[];
  hoveredNodeId?: string;
  selectedNodeId?: string;
};

export function buildChartScene(chart: ChartState, width: number, height: number, options: ChartSceneOptions = {}): ChartScene {
  const safeWidth = Math.max(1, width);
  const safeHeight = Math.max(1, height);
  const padding = {
    top: safeHeight < 240 ? 62 : 84,
    right: priceAxisWidth,
    bottom: chart.layers.volume ? 36 : 30,
    left: 0
  };
  const volumeRatio = Math.max(0.1, Math.min(0.45, chart.volumeRatio || 0.2));
  const availablePlotHeight = Math.max(1, safeHeight - padding.top - padding.bottom);
  const rawVolumeHeight = Math.max(42, Math.floor(safeHeight * volumeRatio));
  const volumeHeight = chart.layers.volume && availablePlotHeight >= 92
    ? Math.min(availablePlotHeight - 46, rawVolumeHeight)
    : 0;
  const priceBottom = Math.max(padding.top + 26, safeHeight - padding.bottom - volumeHeight);
  const plot: ChartPlot = {
    left: padding.left,
    right: safeWidth - padding.right,
    top: padding.top,
    bottom: safeHeight - padding.bottom,
    priceBottom,
    volumeTop: volumeHeight > 0 ? priceBottom + 10 : priceBottom
  };
  const plotWidth = Math.max(1, plot.right - plot.left);
  const viewport = normalizeViewport(
    { visibleCount: chart.visibleCount, rightOffset: chart.rightOffset },
    chart.candles.length,
    plotWidth
  );
  const viewportEndIndex = Math.max(0, chart.candles.length - viewport.rightOffset);
  const viewportStartIndex = viewportEndIndex - viewport.visibleCount;
  const visibleStartIndex = Math.max(0, Math.min(chart.candles.length, Math.floor(viewportStartIndex)));
  const visibleEndIndex = Math.max(visibleStartIndex, Math.min(chart.candles.length, Math.ceil(viewportEndIndex)));
  const semanticBase = buildSemanticTimeline({
    symbol: chart.symbol,
    interval: chart.interval,
    candles: chart.candles,
    expansions: options.expansions ?? [],
    visibleStartIndex,
    visibleEndIndex,
    viewportStartIndex,
    visibleSlotCount: viewport.visibleCount
  });
  const candles = semanticBase.units.filter((unit): unit is Extract<SemanticRenderUnit, { kind: "candle" }> => unit.kind === "candle").map((unit) => unit.candle);
  const priceRange = priceDomain(candles, chart);
  const maxVolume = Math.max(1, ...candles.map((candle) => candle.volume));
  const volumeRange = volumeDomain(maxVolume);
  const slotWidth = plotWidth / Math.max(1, semanticBase.totalSlots);
  const candleWidth = slotWidth < 2 ? Math.max(0.3, slotWidth * 0.75) : Math.max(2, Math.min(24, slotWidth * 0.72));
  const expansionRanges = semanticBase.expansionRanges
    .map((range) => ({
      ...range,
      left: slotBoundaryToX(plot, slotWidth, range.slotStart),
      right: slotBoundaryToX(plot, slotWidth, range.slotEnd)
    }))
    .sort((left, right) => left.depth - right.depth || left.slotStart - right.slotStart);
  return {
    width: safeWidth,
    height: safeHeight,
    chart,
    allCandles: chart.candles,
    candles,
    visibleStartIndex,
    visibleEndIndex,
    viewportStartIndex,
    viewportEndIndex,
    visibleSlotCount: viewport.visibleCount,
    semantic: {
      ...semanticBase,
      expansionRanges
    },
    hoveredNodeId: options.hoveredNodeId,
    selectedNodeId: options.selectedNodeId,
    plot,
    scales: {
      minPrice: priceRange.min,
      maxPrice: priceRange.max,
      priceTicks: priceRange.ticks,
      maxVolume: volumeRange.max,
      volumeTicks: volumeRange.ticks,
      slotWidth,
      candleWidth
    }
  };
}

export function createCoordinateTransform(scene: ChartScene): CoordinateTransform {
  const timestampIndex = new Map(scene.allCandles.map((candle, index) => [candle.timestamp, index]));
  const priceToY = (price: number) => {
    const range = Math.max(0.0001, scene.scales.maxPrice - scene.scales.minPrice);
    return scene.plot.top + ((scene.scales.maxPrice - price) / range) * Math.max(1, scene.plot.priceBottom - scene.plot.top);
  };
  const yToPrice = (y: number) => {
    const range = Math.max(0.0001, scene.scales.maxPrice - scene.scales.minPrice);
    return scene.scales.maxPrice - ((y - scene.plot.top) / Math.max(1, scene.plot.priceBottom - scene.plot.top)) * range;
  };
  const logicalToX = (logicalIndex: number) => {
    const semanticSlot = scene.semantic.logicalIndexToSlot.get(logicalIndex);
    const slotCenter = typeof semanticSlot === "number" ? semanticSlot : logicalIndex - scene.viewportStartIndex + 0.5;
    return slotCenterToX(scene, slotCenter);
  };
  const xToLogical = (x: number) => (
    scene.viewportStartIndex + (x - scene.plot.left - scene.scales.slotWidth / 2) / Math.max(0.0001, scene.scales.slotWidth)
  );
  const timestampToX = (timestamp: string) => {
    const semanticSlot = scene.semantic.timestampToSlot.get(timestamp);
    if (typeof semanticSlot === "number") {
      return slotCenterToX(scene, semanticSlot);
    }
    const logicalIndex = timestampIndex.get(timestamp);
    return typeof logicalIndex === "number" ? logicalToX(logicalIndex) : null;
  };

  return {
    priceToY,
    yToPrice,
    logicalToX,
    xToLogical,
    timestampToX,
    anchorToPoint: (anchor) => {
      const value = anchor.price;
      const x = typeof anchor.logicalIndex === "number"
        ? logicalToX(anchor.logicalIndex)
        : anchor.timestamp ? timestampToX(anchor.timestamp) : scene.plot.left;
      if (typeof x !== "number") {
        return null;
      }
      return { x, y: typeof value === "number" ? priceToY(value) : scene.plot.top };
    },
    pointToAnchor: (x, y, symbol) => {
      if (x < scene.plot.left || x > scene.plot.right || y < scene.plot.top || y > scene.plot.priceBottom) {
        return null;
      }
      const semanticHit = hitTestSemanticNode(scene, x, y);
      if (semanticHit?.kind === "candle") {
        return {
          timestamp: semanticHit.timestamp,
          logicalIndex: semanticHit.sourceIndex,
          price: yToPrice(y),
          paneId: "price",
          symbol
        };
      }
      const logicalIndex = Math.max(0, Math.min(Math.max(scene.viewportEndIndex - 1, scene.allCandles.length - 1), Math.round(xToLogical(x))));
      const candle = scene.allCandles[logicalIndex];
      if (!candle && logicalIndex < scene.allCandles.length) {
        return null;
      }
      return {
        timestamp: candle?.timestamp,
        logicalIndex,
        price: yToPrice(y),
        paneId: "price",
        symbol
      };
    }
  };
}

export function slotCenterToX(scene: Pick<ChartScene, "plot" | "scales">, slotCenter: number): number {
  return scene.plot.left + slotCenter * scene.scales.slotWidth;
}

export function unitCenterX(scene: ChartScene, unit: SemanticRenderUnit): number {
  return slotCenterToX(scene, unit.slotCenter);
}

export function unitBoundsX(scene: ChartScene, unit: SemanticRenderUnit): { left: number; right: number; center: number } {
  return {
    left: slotBoundaryToX(scene.plot, scene.scales.slotWidth, unit.slotStart),
    right: slotBoundaryToX(scene.plot, scene.scales.slotWidth, unit.slotEnd),
    center: unitCenterX(scene, unit)
  };
}

export function findSemanticUnit(scene: ChartScene, nodeId: string | undefined): SemanticRenderUnit | undefined {
  return nodeId ? scene.semantic.unitById.get(nodeId) : undefined;
}

export function hitTestSemanticNode(scene: ChartScene, x: number, y: number): SemanticRenderUnit | null {
  if (x < scene.plot.left || x > scene.plot.right || y < scene.plot.top || y > scene.plot.bottom) {
    return null;
  }
  let best: SemanticRenderUnit | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  scene.semantic.units.forEach((unit) => {
    const bounds = unitBoundsX(scene, unit);
    if (x < bounds.left || x > bounds.right) {
      return;
    }
    const distance = Math.abs(x - bounds.center);
    if (distance < bestDistance) {
      best = unit;
      bestDistance = distance;
    }
  });
  return best;
}

function priceDomain(candles: CandleDto[], chart: ChartState): { min: number; max: number; ticks: number[] } {
  const values = candles.flatMap((candle) => [
    candle.high,
    candle.low,
    chart.layers.ma5 ? candle.ma5 : undefined,
    chart.layers.ma20 ? candle.ma20 : undefined,
    chart.layers.ma60 ? candle.ma60 : undefined
  ]).filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  chart.drawings.forEach((drawing) => {
    drawing.anchors.forEach((anchor) => {
      if (typeof anchor.price === "number") {
        values.push(anchor.price);
      }
    });
  });
  if (!values.length) {
    return { min: 0, max: 4, ticks: [0, 1, 2, 3, 4] };
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const rawRange = Math.max(0.01, max - min);
  const pad = Math.max(0.5, rawRange * 0.08);
  return integerPriceDomain(min - pad, max + pad);
}

function integerPriceDomain(min: number, max: number): { min: number; max: number; ticks: number[] } {
  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    return { min: 0, max: 4, ticks: [0, 1, 2, 3, 4] };
  }
  if (max <= min) {
    const center = Math.round(max || min || 0);
    return { min: center - 2, max: center + 2, ticks: [center - 2, center - 1, center, center + 1, center + 2] };
  }
  const targetGaps = 4;
  let step = niceIntegerStep((max - min) / targetGaps);
  let domainMin = Math.floor(min / step) * step;
  let domainMax = Math.ceil(max / step) * step;
  let tickCount = Math.round((domainMax - domainMin) / step) + 1;
  while (tickCount > 7) {
    step = niceIntegerStep(step * 1.5);
    domainMin = Math.floor(min / step) * step;
    domainMax = Math.ceil(max / step) * step;
    tickCount = Math.round((domainMax - domainMin) / step) + 1;
  }
  while (tickCount < 3) {
    domainMin -= step;
    domainMax += step;
    tickCount = Math.round((domainMax - domainMin) / step) + 1;
  }
  const ticks: number[] = [];
  for (let value = domainMin; value <= domainMax + step / 2; value += step) {
    ticks.push(Math.round(value));
  }
  return { min: domainMin, max: domainMax, ticks };
}

function niceIntegerStep(rawStep: number): number {
  if (!Number.isFinite(rawStep) || rawStep <= 1) {
    return 1;
  }
  const exponent = Math.floor(Math.log10(rawStep));
  const magnitude = 10 ** exponent;
  const normalized = rawStep / magnitude;
  const nice = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return Math.max(1, Math.round(nice * magnitude));
}

function volumeDomain(maxVolume: number): { max: number; ticks: number[] } {
  const paddedMax = Math.max(1, maxVolume * volumeScalePadding);
  const top = niceIntegerCeil(paddedMax);
  const middle = Math.round(top / 2);
  return { max: top, ticks: [0, middle, top] };
}

function niceIntegerCeil(value: number): number {
  if (!Number.isFinite(value) || value <= 1) {
    return 1;
  }
  const exponent = Math.floor(Math.log10(value));
  const magnitude = 10 ** Math.max(0, exponent - 1);
  return Math.max(1, Math.ceil(value / magnitude) * magnitude);
}

function slotBoundaryToX(plot: ChartPlot, slotWidth: number, slot: number): number {
  return plot.left + slot * slotWidth;
}
