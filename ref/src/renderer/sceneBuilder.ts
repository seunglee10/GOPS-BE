import type { CalculationOutput, IndicatorPoint, IndicatorSeries } from "../types/calculations";
import type {
  ChartDocument,
  ChartId,
  ChartViewport,
  DrawingLayer,
  LayerDocument,
  LayerId,
  LayerStyle,
  LayerType,
  PaneId
} from "../types/documents";
import type { Candle, SymbolCode, Timeframe } from "../types/market";
import type { CandlesBySymbol } from "../market/candleStore";
import { defaultLayerRendererRegistry, type LayerRendererRegistry } from "./layerRendererRegistry";
import { paddedDomain } from "./scales";
import { resolveVisibleIndexRange } from "./timeScale";
import { createTimeScaleModel, type TimeScaleModel } from "./timeScaleModel";
import { createValueScaleModel, type ValueScaleModel } from "./valueScaleModel";

export interface RenderSize {
  width: number;
  height: number;
  devicePixelRatio: number;
}

export interface RenderScene {
  chartId: ChartId;
  symbol: SymbolCode;
  timeframe: Timeframe;
  viewport: ChartViewport;
  size: RenderSize;
  panes: RenderPane[];
  layers: RenderLayer[];
  interaction: RenderInteractionState;
  timeScale: TimeScaleModel;
  crosshairReadout?: RenderCrosshairReadout;
  generatedAt: string;
  logicalRange: { from: number; to: number };
  visibleDataRange: { from: number; toExclusive: number };
  visibleCandles: Candle[];
}

export interface RenderPane {
  id: PaneId;
  kind: "price" | "volume" | "indicator";
  bounds: RenderBounds;
  xScale: TimeScaleModel;
  yScale: ValueScaleModel;
  comparisonScale?: ValueScaleModel;
  comparisonBases: ComparisonScaleBase[];
  title: string;
}

export interface RenderBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export type RenderScale = ValueScaleModel;

export interface RenderInteractionState {
  crosshair?: {
    x: number;
    y: number;
    paneId: PaneId;
  };
  selectedLayerId?: LayerId;
  hoveredLayerId?: LayerId;
  drawingPreview?: RenderDrawingLayer;
}

export interface RenderCrosshairReadout {
  paneId: PaneId;
  x: number;
  y: number;
  logical: number;
  timestamp?: string;
  value: number;
  valueLabel: string;
}

export interface ComparisonScaleBase {
  layerId: LayerId;
  symbol: SymbolCode;
  mode: string;
  timestamp?: string;
  close?: number;
}

export type RenderLayer =
  | RenderPriceSeriesLayer
  | RenderVolumeLayer
  | RenderIndicatorLayer
  | RenderComparisonLayer
  | RenderDrawingLayer
  | RenderAiProposalLayer;

export interface BaseRenderLayer {
  id: LayerId;
  type: LayerType;
  paneId: PaneId;
  zIndex: number;
  visible: boolean;
  style: LayerStyle;
  selected?: boolean;
}

export interface RenderPriceSeriesLayer extends BaseRenderLayer {
  type: "priceSeries";
  seriesType: "candlestick" | "ohlc" | "line" | "area";
  candles: Candle[];
}

export interface RenderVolumeLayer extends BaseRenderLayer {
  type: "volume";
  candles: Candle[];
}

export interface RenderIndicatorLayer extends BaseRenderLayer {
  type: "indicator";
  series: IndicatorSeries[];
}

export interface RenderComparisonLayer extends BaseRenderLayer {
  type: "comparisonSeries";
  symbol: SymbolCode;
  points: IndicatorPoint[];
  baseTimestamp?: string;
  baseClose?: number;
  baselineMode: string;
}

export interface RenderDrawingLayer extends BaseRenderLayer {
  type: "drawing";
  drawing: DrawingLayer["drawing"];
}

export interface RenderAiProposalLayer extends BaseRenderLayer {
  type: "aiProposal";
  proposalId: string;
  previewLayers: RenderLayer[];
}

export interface BuildRenderSceneArgs {
  chart: ChartDocument;
  candlesBySymbol: CandlesBySymbol;
  calculationOutputs: Record<string, CalculationOutput>;
  size: RenderSize;
  interaction?: RenderInteractionState;
  registry?: LayerRendererRegistry;
}

export interface BuildRenderLayerArgs {
  layer: LayerDocument;
  chart: ChartDocument;
  visibleCandles: Candle[];
  candlesBySymbol: CandlesBySymbol;
  calculationOutputs: Record<string, CalculationOutput>;
}

export function buildRenderScene({
  chart,
  candlesBySymbol,
  calculationOutputs,
  size,
  interaction = {},
  registry = defaultLayerRendererRegistry
}: BuildRenderSceneArgs): RenderScene {
  const sourceCandles = candlesBySymbol[chart.symbol] ?? [];
  const visibleSelection = selectVisibleCandleRange(sourceCandles, chart.viewport);
  const visibleCandles = visibleSelection.candles;
  const baseLayers = chart.layers
    .filter((layer) => layer.visible)
    .flatMap((layer) => {
      const definition = registry[layer.type];
      const renderLayer = definition?.buildRenderLayer({
        layer,
        chart,
        visibleCandles,
        candlesBySymbol,
        calculationOutputs
      });
      return renderLayer ? [renderLayer] : [];
    });
  const renderLayers = [...baseLayers, ...(interaction.drawingPreview ? [interaction.drawingPreview] : [])]
    .map((layer) => ({ ...layer, selected: layer.selected || interaction.selectedLayerId === layer.id }))
    .sort(layerSort);
  const panes = buildPanes(chart, size, visibleSelection, visibleCandles, renderLayers);
  const timeScale =
    panes[0]?.xScale ??
    createTimeScaleModel({
      bounds: { x: 0, width: size.width },
      logicalRange: { from: visibleSelection.logicalFrom, to: visibleSelection.logicalTo },
      visibleDataRange: { from: visibleSelection.from, toExclusive: visibleSelection.toExclusive },
      visibleCandles
    });
  return {
    chartId: chart.id,
    symbol: chart.symbol,
    timeframe: chart.timeframe,
    viewport: chart.viewport,
    size,
    panes,
    layers: renderLayers,
    interaction,
    timeScale,
    crosshairReadout: buildCrosshairReadout(interaction, panes, visibleCandles),
    generatedAt: new Date().toISOString(),
    logicalRange: { from: visibleSelection.logicalFrom, to: visibleSelection.logicalTo },
    visibleDataRange: { from: visibleSelection.from, toExclusive: visibleSelection.toExclusive },
    visibleCandles
  };
}

export function selectVisibleCandles(candles: Candle[], viewport: ChartViewport): Candle[] {
  return selectVisibleCandleRange(candles, viewport).candles;
}

export function selectVisibleCandleRange(
  candles: Candle[],
  viewport: ChartViewport
): {
  candles: Candle[];
  from: number;
  toExclusive: number;
  logicalFrom: number;
  logicalTo: number;
} {
  if (candles.length === 0) return { candles: [], from: 0, toExclusive: 0, logicalFrom: 0, logicalTo: 0 };
  if (viewport.mode === "fixedRange" && viewport.from && viewport.to) {
    const from = candles.findIndex((candle) => candle.timestamp >= viewport.from!);
    const last = findLastIndex(candles, (candle) => candle.timestamp <= viewport.to!);
    if (from < 0 || last < from) return { candles: [], from: 0, toExclusive: 0, logicalFrom: 0, logicalTo: 0 };
    return {
      candles: candles.slice(from, last + 1),
      from,
      toExclusive: last + 1,
      logicalFrom: from,
      logicalTo: last
    };
  }
  const range = resolveVisibleIndexRange(candles.length, viewport);
  return {
    candles: candles.slice(range.from, range.toExclusive),
    from: range.from,
    toExclusive: range.toExclusive,
    logicalFrom: range.logicalFrom,
    logicalTo: range.logicalTo
  };
}

function buildPanes(
  chart: ChartDocument,
  size: RenderSize,
  visibleSelection: ReturnType<typeof selectVisibleCandleRange>,
  candles: Candle[],
  layers: RenderLayer[]
): RenderPane[] {
  const visiblePanes = chart.panes.filter((pane) => pane.visible).sort((a, b) => a.order - b.order);
  const topPadding = 24;
  const bottomPadding = 8;
  const leftPadding = 10;
  const rightPadding = 68;
  const innerHeight = Math.max(120, size.height - topPadding - bottomPadding);
  const totalRatio = visiblePanes.reduce((total, pane) => total + pane.heightRatio, 0) || 1;
  let y = topPadding;
  return visiblePanes.map((pane, index) => {
    const height =
      index === visiblePanes.length - 1
        ? size.height - bottomPadding - y
        : Math.max(pane.minHeightPx * 0.35, (innerHeight * pane.heightRatio) / totalRatio);
    const bounds = {
      x: leftPadding,
      y,
      width: Math.max(160, size.width - leftPadding - rightPadding),
      height: Math.max(80, height)
    };
    y += bounds.height;
    const values = valuesForPane(pane.id, pane.kind, candles, layers);
    const domain: [number, number] = pane.yScale.autoScale
      ? paddedDomain(values, fallbackDomain(pane.kind, candles))
      : [pane.yScale.min ?? 0, pane.yScale.max ?? 1];
    const comparisonDomain = comparisonDomainForPane(pane.id, layers);
    const xScale = createTimeScaleModel({
      bounds,
      logicalRange: { from: visibleSelection.logicalFrom, to: visibleSelection.logicalTo },
      visibleDataRange: { from: visibleSelection.from, toExclusive: visibleSelection.toExclusive },
      visibleCandles: candles
    });
    const yScale = createValueScaleModel({
      scaleId: pane.yScale.scaleId,
      mode: pane.yScale.mode,
      domain,
      bounds
    });
    const comparisonScale = comparisonDomain
      ? createValueScaleModel({
          scaleId: `${pane.yScale.scaleId}-comparison`,
          mode: "percent",
          domain: comparisonDomain,
          bounds
        })
      : undefined;
    return {
      id: pane.id,
      kind: pane.kind,
      title: pane.title,
      bounds,
      xScale,
      yScale,
      comparisonScale,
      comparisonBases: comparisonBasesForPane(pane.id, layers)
    };
  });
}

function buildCrosshairReadout(
  interaction: RenderInteractionState,
  panes: RenderPane[],
  candles: Candle[]
): RenderCrosshairReadout | undefined {
  if (!interaction.crosshair) return undefined;
  const pane = panes.find((item) => item.id === interaction.crosshair?.paneId);
  if (!pane) return undefined;
  const logical = pane.xScale.xToLogical(interaction.crosshair.x);
  const visibleIndex = Math.round(logical) - pane.xScale.visibleDataRange.from;
  const candle = candles[visibleIndex];
  const value = pane.yScale.yToValue(interaction.crosshair.y);
  return {
    paneId: pane.id,
    x: interaction.crosshair.x,
    y: interaction.crosshair.y,
    logical,
    timestamp: candle?.timestamp,
    value,
    valueLabel: pane.yScale.formatValue(value)
  };
}

function findLastIndex<T>(items: T[], predicate: (item: T) => boolean): number {
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (predicate(items[index])) return index;
  }
  return -1;
}

function valuesForPane(paneId: PaneId, kind: RenderPane["kind"], candles: Candle[], layers: RenderLayer[]): number[] {
  const values: number[] = [];
  if (kind === "price") {
    for (const candle of candles) values.push(candle.low, candle.high);
  }
  if (kind === "volume") {
    values.push(0);
    for (const candle of candles) values.push(candle.volume);
  }
  for (const layer of layers) {
    if (layer.paneId !== paneId) continue;
    if (layer.type === "indicator") {
      for (const series of layer.series) {
        for (const point of series.points) {
          if (typeof point.value === "number") values.push(point.value);
          if (point.values) {
            for (const value of Object.values(point.values)) if (typeof value === "number") values.push(value);
          }
        }
      }
    } else if (layer.type === "drawing" && layer.drawing.kind === "horizontalLine") {
      values.push(layer.drawing.price);
    }
  }
  return values;
}

function fallbackDomain(kind: RenderPane["kind"], candles: Candle[]): [number, number] {
  if (kind === "volume") return [0, Math.max(1, ...candles.map((candle) => candle.volume))];
  if (candles.length === 0) return [0, 1];
  return [Math.min(...candles.map((candle) => candle.low)), Math.max(...candles.map((candle) => candle.high))];
}

function comparisonDomainForPane(paneId: PaneId, layers: RenderLayer[]): [number, number] | undefined {
  const values = layers.flatMap((layer) => {
    if (layer.paneId !== paneId || layer.type !== "comparisonSeries") return [];
    const stablePoints = layer.points.length > 1 ? layer.points.slice(0, -1) : layer.points;
    return stablePoints.flatMap((point) => (typeof point.value === "number" && Number.isFinite(point.value) ? [point.value] : []));
  });
  return values.length > 0 ? paddedDomain(values, [-1, 1]) : undefined;
}

function comparisonBasesForPane(paneId: PaneId, layers: RenderLayer[]): ComparisonScaleBase[] {
  return layers.flatMap((layer) => {
    if (layer.paneId !== paneId || layer.type !== "comparisonSeries") return [];
    return [
      {
        layerId: layer.id,
        symbol: layer.symbol,
        mode: layer.baselineMode,
        timestamp: layer.baseTimestamp,
        close: layer.baseClose
      }
    ];
  });
}

function layerSort(a: RenderLayer, b: RenderLayer): number {
  const order: Record<LayerType, number> = {
    priceSeries: 1,
    volume: 2,
    comparisonSeries: 3,
    indicator: 4,
    drawing: 5,
    aiProposal: 6
  };
  return order[a.type] - order[b.type] || a.zIndex - b.zIndex;
}
