export type CandleData = {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  isClosed: boolean;
  sourceInterval?: string;
  feedProfile?: string;
  marketSession?: string;
  updatedAt?: string;
  ma5?: number;
  ma20?: number;
  ma60?: number;
};

export type CandleEventType = "LIVE_CANDLE_UPDATE" | "CANDLE_CLOSED" | "CANDLE_CORRECTED";
export type ChartSnapshotDataStatus = "ready" | "partial" | "empty" | "error";
export type BackfillStatus = "not_requested" | "queued" | "running" | "succeeded" | "failed" | "unavailable";
export type RepairStatus = "none" | "gapfill_required" | "gapfill_active" | "gapfill_failed";
export type ChartCoverageState = "complete" | "partial" | "empty" | "unavailable";
export type ChartRequestedRange = {
  before?: string;
  from?: string;
  to?: string;
};

export type ChartCoverage = {
  state: ChartCoverageState;
  reasonCode?: string;
  message?: string;
  repairStatus?: RepairStatus;
  sourceInterval?: string;
  backfillStatus?: BackfillStatus;
  requestedLimit?: number;
  returnedCount?: number;
  storedCandleCount?: number;
  availableFrom?: string;
  availableTo?: string;
  noDataBefore?: string;
  requestedRange?: ChartRequestedRange;
  invalidRowCount?: number;
  renderable?: boolean;
  minimumReturnedCount?: number;
  minimumRenderableSourceBars?: number;
  expectedRequestedRangeBars?: number;
  returnedSpanSeconds?: number;
  maxRenderableSpanSeconds?: number;
  renderabilityReasonCode?: string;
};

export type CandleSnapshot = {
  symbol: string;
  interval: string;
  source: string;
  feed: string;
  feedProfile?: string;
  marketSession?: string;
  snapshotCursor?: string;
  dataStatus?: ChartSnapshotDataStatus;
  backfillStatus?: BackfillStatus;
  repairStatus?: RepairStatus;
  canBackfill?: boolean;
  sourceInterval?: string;
  message?: string;
  requestedLimit?: number;
  returnedCount?: number;
  storedCandleCount?: number;
  availableFrom?: string;
  availableTo?: string;
  noDataBefore?: string;
  requestedRange?: ChartRequestedRange;
  oldestTimestamp?: string;
  newestTimestamp?: string;
  hasMoreBefore?: boolean;
  hasMoreAfter?: boolean;
  coverage?: ChartCoverage;
  indicators: {
    ma: number[];
    volume: boolean;
  };
  candles: CandleData[];
};

export type CandleEvent = {
  type: CandleEventType;
  eventId?: string;
  cursor?: string;
  symbol: string;
  interval: string;
  sourceInterval?: string;
  source?: string;
  feed?: string;
  feedProfile?: string;
  marketSession?: string;
  data: CandleData;
};

export type StreamStatus = "connecting" | "idle" | "live" | "stale" | "error";

export type ChartLayerKey = "candles" | "volume" | "ma5" | "ma20" | "ma60";

export type ChartSizeVariant = "compact" | "standard" | "wide" | "large";

export type ChartViewport = {
  rightOffset: number;
  visibleCount: number;
};

export type ChartCommandActor = "user" | "llm" | "system";

export type ChartCommandHistoryScope = "chartPanel" | "external";

export type ChartCommandType =
  | "chart.symbol.set"
  | "chart.timeframe.set"
  | "chart.viewport.set"
  | "chart.layer.visibility.set"
  | "chart.undo"
  | "chart.redo"
  | "chart.drawing.add"
  | "chart.drawing.update"
  | "chart.drawing.remove"
  | "chart.drawing.select"
  | "chart.drawing.clearSelection"
  | "chart.preview.set"
  | "chart.preview.toggle"
  | "chart.preview.apply"
  | "chart.preview.clear"
  | "chart.comparison.add"
  | "chart.comparison.remove"
  | "chart.comparison.update"
  | "chart.measurement.add";

export type ChartCommand = {
  id: string;
  type: ChartCommandType;
  actor: ChartCommandActor;
  target: {
    panelId: string;
    chartDocumentId: string;
  };
  payload: Record<string, unknown>;
  createdAt: string;
  proposalId?: string;
  historyScope?: ChartCommandHistoryScope;
};

export type ChartCommandJournalEntry = {
  id: string;
  commandType: ChartCommandType | "chart.proposal.accept" | "chart.proposal.reject" | "chart.data.snapshot" | "chart.data.live";
  actor: ChartCommandActor;
  status: "applied" | "failed" | "proposed" | "ignored" | "undone" | "redone";
  message: string;
  chartDocumentId?: string;
  createdAt: string;
};

export type ChartDocumentSnapshot = {
  id: string;
  symbol: string;
  timeframe: string;
  viewport: ChartViewport;
  panes: ChartDocument["panes"];
  layers: ChartDocument["layers"];
  style: ChartDocument["style"];
  interactionState: ChartDocument["interactionState"];
  drawings: DrawingEntity[];
  comparisons: ComparisonSeries[];
  selectedDrawingId?: string;
  updatedAt: string;
};

export type ChartHistoryEntry = {
  id: string;
  label: string;
  commandTypes: ChartCommandType[];
  actor: ChartCommandActor;
  before: ChartDocumentSnapshot;
  after: ChartDocumentSnapshot;
  createdAt: string;
  proposalId?: string;
  historyScope?: ChartCommandHistoryScope;
};

export type ChartDocument = {
  id: string;
  symbol: string;
  timeframe: string;
  viewport: ChartViewport;
  panes: Array<{
    id: "price" | "volume";
    heightRatio: number;
  }>;
  layers: Record<ChartLayerKey, boolean>;
  style: {
    background: string;
    grid: string;
    text: string;
    bullish: string;
    bearish: string;
    ma5: string;
    ma20: string;
    ma60: string;
    volume: string;
  };
  interactionState: {
    mode: ChartToolMode;
    trendLineExtension: ChartLineExtension;
  };
  drawings: DrawingEntity[];
  comparisons: ComparisonSeries[];
  selectedDrawingId?: string;
  history: ChartHistoryEntry[];
  future: ChartHistoryEntry[];
  updatedAt: string;
};

export type ChartLoadState = "loading" | "ready" | "partial" | "empty" | "error";

export type ChartDataStatus = {
  state: ChartLoadState;
  message?: string;
  source?: string;
  feed?: string;
  feedProfile?: string;
  marketSession?: string;
  backfillStatus?: BackfillStatus;
  repairStatus?: RepairStatus;
  canBackfill?: boolean;
  sourceInterval?: string;
  requestedLimit?: number;
  returnedCount?: number;
  storedCandleCount?: number;
  availableFrom?: string;
  availableTo?: string;
  noDataBefore?: string;
  requestedRange?: ChartRequestedRange;
  oldestTimestamp?: string;
  newestTimestamp?: string;
  hasMoreBefore?: boolean;
  hasMoreAfter?: boolean;
  coverage?: ChartCoverage;
  updatedAt: string;
};

export type ChartProposalStatus = "pending" | "applied" | "rejected" | "failed";

export type ChartProposal = {
  id: string;
  title: string;
  rationale: string;
  summary: string;
  target: {
    panelId: string;
    chartDocumentId: string;
  };
  commands: ChartCommand[];
  insights: string[];
  status: ChartProposalStatus;
  createdAt: string;
  createdByAgentId: string;
  error?: string;
};

export type ChartCapability = {
  id: string;
  label: string;
  description: string;
  commandTypes: ChartCommandType[];
  payloadSchema: Record<string, unknown>;
  requiredContext: string[];
  previewable: boolean;
  autoApplyEligible: boolean;
  undoScope: "chart" | "none";
  conflictsWith: string[];
  recommendedWith: string[];
  validationRules: string[];
};

export type ChartRuntimeError = {
  id: string;
  message: string;
  chartDocumentId?: string;
  createdAt: string;
};

export type ChartRuntimeState = {
  documents: Record<string, ChartDocument>;
  candlesByKey: Record<string, CandleData[]>;
  dataStatusByKey: Record<string, ChartDataStatus>;
  streamStatusByKey: Record<string, StreamStatus>;
  streamMessageByKey?: Record<string, string>;
  pendingPreviewByDocumentId: Record<string, ChartPendingPreview>;
  pendingProposals: ChartProposal[];
  journal: ChartCommandJournalEntry[];
  errors: ChartRuntimeError[];
};

export type ChartToolMode =
  | "select"
  | "pan"
  | "draw-horizontalLine"
  | "draw-trendLine"
  | "draw-verticalMarker"
  | "draw-textLabel"
  | "draw-pointMarker"
  | "draw-arrow"
  | "draw-rangeBox"
  | "draw-measurement";

export type DrawingType =
  | "horizontalLine"
  | "trendLine"
  | "verticalMarker"
  | "textLabel"
  | "pointMarker"
  | "arrow"
  | "rangeBox"
  | "measurement"
  | "ellipse"
  | "riskRewardBox"
  | "fibonacciRetracement";

export type DrawingAnchor = {
  timestamp?: string;
  price?: number;
  paneId?: "price" | "volume" | string;
  symbol?: string;
  logicalIndex?: number;
  value?: number;
};

export type ChartLineExtension = "segment" | "ray" | "line";

export type DrawingStyle = {
  color?: string;
  lineWidth?: number;
  lineDash?: number[];
  fillColor?: string;
  textColor?: string;
  fontSize?: number;
  opacity?: number;
  extension?: ChartLineExtension;
};

export type DrawingEntity = {
  id: string;
  type: DrawingType;
  anchors: DrawingAnchor[];
  style: DrawingStyle;
  label?: string;
  locked?: boolean;
  visible: boolean;
  createdBy: ChartCommandActor;
  sourceProposalId?: string;
  createdAt: string;
  updatedAt: string;
};

export type ComparisonSeries = {
  id: string;
  symbol: string;
  label?: string;
  scaleMode: "percent";
  base?: {
    mode: "visibleRangeStart" | "timestamp";
    timestamp?: string;
  };
  style: DrawingStyle;
};

export type ChartPendingPreview = {
  id: string;
  sourceProposalId?: string;
  drawings: DrawingEntity[];
  comparisons: ComparisonSeries[];
  rationale?: string;
  confidence?: number;
  visible: boolean;
  createdAt: string;
};

export type ChartCrosshair = {
  x: number;
  y: number;
  candleIndex: number;
  candle: CandleData;
  price: number;
};

export type RenderScene = {
  state: ChartLoadState;
  message?: string;
  width: number;
  height: number;
  document: ChartDocument;
  candles: CandleData[];
  allCandles: CandleData[];
  visibleStartIndex: number;
  visibleEndIndex: number;
  viewportStartIndex: number;
  viewportEndIndex: number;
  visibleSlotCount: number;
  futureSlotCount: number;
  pendingPreview?: ChartPendingPreview;
  variant: ChartSizeVariant;
  crosshair?: ChartCrosshair;
  plot: {
    left: number;
    top: number;
    right: number;
    bottom: number;
    priceBottom: number;
    volumeTop: number;
  };
  scales: {
    minPrice: number;
    maxPrice: number;
    maxVolume: number;
    minPercent: number;
    maxPercent: number;
    slotWidth: number;
    candleWidth: number;
    gap: number;
  };
  comparisonSeries: Array<{
    comparison: ComparisonSeries;
    candles: CandleData[];
    points: Array<{ x: number; y: number; percent: number; candle: CandleData }>;
  }>;
  labels: {
    symbol: string;
    timeframe: string;
    lastPrice?: string;
    range?: string;
    change?: string;
    visibleHigh?: string;
    visibleLow?: string;
    streamStatus?: StreamStatus;
  };
};
