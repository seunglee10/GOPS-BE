from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SymbolCode = str
Timeframe = Literal["1s", "5s", "15s", "1m", "5m", "15m", "1h", "1d"]
Provider = Literal["dummy", "alpaca"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Candle(StrictModel):
    symbol: SymbolCode
    timeframe: Timeframe
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    tradeCount: int | None = None
    finalized: bool


class MarketSnapshotResponse(StrictModel):
    provider: Provider = "dummy"
    timeframe: Timeframe
    generatedAt: str
    symbols: list[SymbolCode]
    candlesBySymbol: dict[SymbolCode, list[Candle]]


class MarketSignal(StrictModel):
    type: Literal[
        "volume_spike",
        "range_expansion",
        "new_visible_high",
        "new_visible_low",
        "ema_cross",
        "rsi_overbought",
        "rsi_oversold",
    ]
    severity: Literal["low", "medium", "high"]
    message: str
    timestamp: str


class MarketSummary(StrictModel):
    symbol: SymbolCode
    timeframe: Timeframe
    latestPrice: float
    latestTimestamp: str
    changePercentFromFirstVisible: float
    visibleChangeBaseTimestamp: str | None = None
    visibleChangeBaseClose: float | None = None
    liveChangePercent: float | None = None
    visibleHigh: float
    visibleLow: float
    visibleVolume: float
    averageVolume: float
    realizedVolatility: float
    trend: Literal["strong_up", "up", "sideways", "down", "strong_down", "insufficient_data"]
    notableSignals: list[MarketSignal]


class WorkspacePanelContext(StrictModel):
    id: str
    type: Literal["chart", "chat", "proposalList"]
    title: str
    pinMode: Literal["locked", "approval", "auto"]
    targetChartId: str | None = None


class WorkspaceContextForLlm(StrictModel):
    activePanelId: str
    activeChartId: str
    panels: list[WorkspacePanelContext]
    pendingProposalCount: int


class ChartViewport(StrictModel):
    mode: Literal["followRealtime", "fixedRange", "fixedLogicalRange"]
    visibleBars: int
    rightOffsetBars: int
    logicalFrom: float | None = None
    logicalTo: float | None = None
    minVisibleBars: int | None = None
    maxVisibleBars: int | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None


class ChartPaneContext(StrictModel):
    id: str
    kind: Literal["price", "volume", "indicator"]
    title: str


class ChartLayerContext(StrictModel):
    id: str
    type: Literal["priceSeries", "volume", "indicator", "comparisonSeries", "drawing", "aiProposal"]
    paneId: str
    owner: Literal["user", "ai", "system"]
    visible: bool
    summary: str


class ChartContextForLlm(StrictModel):
    id: str
    symbol: SymbolCode
    timeframe: Timeframe
    viewport: ChartViewport
    panes: list[ChartPaneContext]
    layers: list[ChartLayerContext]
    availableCommands: list[str]


class ChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=2000)
    workspace: WorkspaceContextForLlm
    chart: ChartContextForLlm
    market: MarketSummary


class LlmInsight(StrictModel):
    title: str
    description: str
    severity: Literal["info", "watch", "important"]
    relatedSymbol: str | None = None


class LlmCommandTarget(StrictModel):
    workspaceId: str
    panelId: str
    chartId: str
    paneId: str | None = None
    layerId: str | None = None


class LlmLayerStyle(StrictModel):
    color: str | None = None
    secondaryColor: str | None = None
    lineWidth: float | None = None
    lineDash: list[float] | None = None
    opacity: float | None = None
    fill: str | None = None
    textColor: str | None = None


class LlmChartPoint(StrictModel):
    timestamp: str
    price: float


class LlmDrawing(StrictModel):
    kind: Literal["horizontalLine", "trendLine", "rectangle", "text"]
    price: float | None = None
    label: str | None = None
    start: LlmChartPoint | None = None
    end: LlmChartPoint | None = None
    anchor: LlmChartPoint | None = None
    text: str | None = None


class LlmCalculationInputs(StrictModel):
    source: Literal["open", "high", "low", "close", "volume"] | None = None
    period: int | None = None
    fastPeriod: int | None = None
    slowPeriod: int | None = None
    signalPeriod: int | None = None
    standardDeviation: float | None = None
    reset: Literal["session", "visibleRange"] | None = None


class LlmCalculationNode(StrictModel):
    id: str | None = None
    type: Literal["SMA", "EMA", "RSI", "MACD", "BOLLINGER_BANDS", "VWAP", "ATR", "VOLUME_MA"]
    inputs: LlmCalculationInputs | None = None
    outputKey: str | None = None


class LlmLayerPayload(StrictModel):
    id: str | None = None
    type: Literal["indicator", "drawing", "comparisonSeries"] | None = None
    owner: Literal["ai"] | None = None
    paneId: str | None = None
    zIndex: int | None = None
    visible: bool | None = None
    locked: bool | None = None
    style: LlmLayerStyle | None = None
    calculationNodeId: str | None = None
    renderMode: Literal["line", "histogram", "band", "cloud", "area"] | None = None
    drawing: LlmDrawing | None = None
    symbol: str | None = None
    baselineMode: Literal["firstVisibleClose", "previousClose", "firstVisibleCompleteBar"] | None = None
    normalization: Literal["percentFromFirstVisibleCompleteBar"] | None = None


class LlmCommandPayload(StrictModel):
    symbol: str | None = None
    timeframe: Timeframe | None = None
    mode: Literal["followRealtime", "fixedRange", "fixedLogicalRange"] | None = None
    visibleBars: int | None = None
    logicalFrom: float | None = None
    logicalTo: float | None = None
    rightOffsetBars: int | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    indicatorType: Literal["SMA", "EMA", "RSI", "MACD", "BOLLINGER_BANDS", "VWAP", "ATR", "VOLUME_MA"] | None = None
    calculationNodeId: str | None = None
    inputs: LlmCalculationInputs | None = None
    layerId: str | None = None
    visible: bool | None = None
    style: LlmLayerStyle | None = None
    node: LlmCalculationNode | None = None
    layer: LlmLayerPayload | None = None
    layerPatch: LlmLayerPayload | None = None
    drawing: LlmDrawing | None = None


class LlmCommand(StrictModel):
    type: str
    target: LlmCommandTarget
    payload: LlmCommandPayload
    reason: str | None = None


class LlmChartProposal(StrictModel):
    title: str
    rationale: str
    previewSummary: str
    commands: list[LlmCommand]


class ChatLlmResponse(StrictModel):
    message: str
    insights: list[LlmInsight]
    chartProposals: list[LlmChartProposal]


class CommandValidationError(StrictModel):
    code: Literal[
        "unknown_command_type",
        "invalid_payload",
        "missing_target",
        "target_not_found",
        "panel_locked",
        "approval_required",
        "document_limit_exceeded",
        "layer_type_not_allowed",
        "calculation_node_not_found",
        "unsafe_ai_command",
    ]
    message: str
    path: str | None = None


class ChartProposalDocument(StrictModel):
    id: str
    source: Literal["llm"] = "llm"
    status: Literal["pending", "accepted", "rejected", "invalid"]
    targetPanelId: str
    targetChartId: str
    title: str
    rationale: str
    previewSummary: str
    commands: list[dict[str, Any]]
    createdAt: str
    validationErrors: list[CommandValidationError]


class ChatResponse(StrictModel):
    id: str
    message: str
    insights: list[LlmInsight]
    chartProposals: list[ChartProposalDocument]
    usage: dict[str, int | None] | None = None
    model: str
    createdAt: str


class ChatError(StrictModel):
    code: Literal[
        "openai_api_key_missing",
        "openai_timeout",
        "openai_request_failed",
        "llm_response_invalid",
        "internal_error",
    ]
    message: str


class ChatErrorResponse(StrictModel):
    error: ChatError
