import type { CalculationGraph, CalculationNodeId, IndicatorType } from "./calculations";
import type { ChartProposalDocument, CommandJournalEntry } from "./commands";
import type { Provider, SymbolCode, Timeframe } from "./market";

export type DocumentId = string;
export type PanelId = string;
export type ChartId = string;
export type PaneId = string;
export type LayerId = string;
export type ScaleId = string;
export type Owner = "user" | "ai" | "system";
export type PinMode = "locked" | "approval" | "auto";

export const DEFAULT_SYMBOL = "AAPL";
export const DEFAULT_TIMEFRAME: Timeframe = "1m";
export const DEFAULT_PIN_MODE: PinMode = "approval";
export const DEFAULT_VISIBLE_BARS = 180;
export const DEFAULT_WATCHLIST = ["AAPL", "MSFT", "NVDA", "TSLA", "SPY"];

export const DOCUMENT_LIMITS = {
  maxCharts: 4,
  maxPanesPerChart: 6,
  maxVisibleLayersPerChart: 64,
  maxIndicatorsPerChart: 20,
  maxComparisonSymbolsPerChart: 3,
  maxDrawingsPerChart: 200
} as const;

export interface WorkspaceDocument {
  id: DocumentId;
  version: number;
  activePanelId: PanelId;
  activeChartId: ChartId;
  panels: PanelDocument[];
  charts: ChartDocument[];
  proposals: ChartProposalDocument[];
  commandJournal: CommandJournalEntry[];
  createdAt: string;
  updatedAt: string;
}

export type PanelType = "chart" | "chat" | "proposalList";

export type ChartToolMode = "select" | "drawHorizontalLine";

export interface PanelDocument {
  id: PanelId;
  type: PanelType;
  title: string;
  pinMode: PinMode;
  owner: Owner;
  layout: PanelLayout;
  targetChartId?: ChartId;
  visible: boolean;
  config?: ChartPanelConfig | ChatPanelConfig | ProposalListPanelConfig;
}

export interface PanelLayout {
  area: "main" | "right" | "bottom" | "tools";
  order: number;
  widthPx?: number;
  heightPx?: number;
  minWidthPx: number;
  minHeightPx: number;
}

export interface ChartPanelConfig {
  chartId: ChartId;
  toolMode: ChartToolMode;
  showCrosshair: boolean;
  toolsCollapsed: boolean;
}

export interface ChatPanelConfig {
  scopedPanelIds: PanelId[];
}

export interface ProposalListPanelConfig {
  scopedChartIds: ChartId[];
}

export interface ChartDocument {
  id: ChartId;
  symbol: SymbolCode;
  timeframe: Timeframe;
  provider: Provider;
  viewport: ChartViewport;
  panes: PaneDocument[];
  layers: LayerDocument[];
  dataBindings: DataBinding[];
  calculationGraph: CalculationGraph;
  style: ChartStyle;
  interactionState: ChartInteractionState;
  createdAt: string;
  updatedAt: string;
}

export interface ChartViewport {
  mode: "followRealtime" | "fixedRange" | "fixedLogicalRange";
  visibleBars: number;
  rightOffsetBars: number;
  logicalFrom?: number;
  logicalTo?: number;
  minVisibleBars?: number;
  maxVisibleBars?: number;
  from?: string;
  to?: string;
}

export interface ChartInteractionState {
  selectedLayerId?: LayerId;
  hoveredLayerId?: LayerId;
  crosshair?: {
    timestamp: string;
    price?: number;
    paneId: PaneId;
  };
}

export interface PaneDocument {
  id: PaneId;
  kind: "price" | "volume" | "indicator";
  title: string;
  order: number;
  heightRatio: number;
  minHeightPx: number;
  yScale: ScaleBinding;
  visible: boolean;
}

export interface ScaleBinding {
  scaleId: ScaleId;
  mode: "price" | "volume" | "percent" | "indexedTo100" | "index" | "oscillator" | "custom";
  position: "left" | "right" | "none";
  autoScale: boolean;
  basePolicy?: "firstVisibleCompleteBar" | "firstVisibleDataPoint" | "sessionOpen" | "pinnedTimestamp";
  pinnedTimestamp?: string;
  min?: number;
  max?: number;
}

export type LayerType =
  | "priceSeries"
  | "volume"
  | "indicator"
  | "comparisonSeries"
  | "drawing"
  | "aiProposal";

export interface BaseLayerDocument {
  id: LayerId;
  type: LayerType;
  owner: Owner;
  paneId: PaneId;
  zIndex: number;
  visible: boolean;
  locked: boolean;
  dataBinding?: DataBindingRef;
  scaleBinding?: ScaleBinding;
  style: LayerStyle;
  createdAt: string;
  updatedAt: string;
}

export type LayerDocument =
  | PriceSeriesLayer
  | VolumeLayer
  | IndicatorLayer
  | ComparisonSeriesLayer
  | DrawingLayer
  | AiProposalLayer;

export interface PriceSeriesLayer extends BaseLayerDocument {
  type: "priceSeries";
  seriesType: "candlestick" | "ohlc" | "line" | "area";
}

export interface VolumeLayer extends BaseLayerDocument {
  type: "volume";
  volumeMode: "bar" | "area";
}

export interface IndicatorLayer extends BaseLayerDocument {
  type: "indicator";
  calculationNodeId: CalculationNodeId;
  renderMode: "line" | "histogram" | "band" | "cloud";
}

export interface ComparisonSeriesLayer extends BaseLayerDocument {
  type: "comparisonSeries";
  symbol: SymbolCode;
  baselineMode: "firstVisibleClose" | "previousClose" | "firstVisibleCompleteBar";
  normalization?: "percentFromFirstVisibleCompleteBar";
  renderMode: "line" | "area";
}

export interface DrawingLayer extends BaseLayerDocument {
  type: "drawing";
  drawing: HorizontalLineDrawing | TrendLineDrawing | RectangleDrawing | TextDrawing;
}

export interface AiProposalLayer extends BaseLayerDocument {
  type: "aiProposal";
  proposalId: string;
  previewOnly: true;
}

export interface HorizontalLineDrawing {
  kind: "horizontalLine";
  price: number;
  label?: string;
}

export interface TrendLineDrawing {
  kind: "trendLine";
  start: ChartPoint;
  end: ChartPoint;
  label?: string;
}

export interface RectangleDrawing {
  kind: "rectangle";
  start: ChartPoint;
  end: ChartPoint;
  label?: string;
}

export interface TextDrawing {
  kind: "text";
  anchor: ChartPoint;
  text: string;
}

export interface ChartPoint {
  timestamp: string;
  price: number;
}

export interface DataBinding {
  id: string;
  source: "marketCandles" | "calculation" | "proposalPreview";
  symbol: SymbolCode;
  timeframe: Timeframe;
  calculationNodeId?: CalculationNodeId;
}

export interface DataBindingRef {
  bindingId: string;
}

export interface ChartStyle {
  theme: "dark";
  backgroundColor: string;
  gridColor: string;
  textColor: string;
  upColor: string;
  downColor: string;
  accentColor: string;
}

export interface LayerStyle {
  color?: string;
  secondaryColor?: string;
  lineWidth?: number;
  lineDash?: number[];
  opacity?: number;
  fill?: string;
  textColor?: string;
}

export type { IndicatorType };
