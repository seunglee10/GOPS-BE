import {
  ArrowUpRight,
  Check,
  ChevronDown,
  ChevronUp,
  CircleDot,
  Eraser,
  Hand,
  Eye,
  EyeOff,
  Minus,
  MousePointer2,
  Palette,
  Ruler,
  Square,
  Trash2,
  Type,
  X
} from "lucide-react";
import { FormEvent, PointerEvent as ReactPointerEvent, useCallback, useEffect, useMemo, useRef, useState, WheelEvent as ReactWheelEvent } from "react";
import { createPortal } from "react-dom";
import { requestChartAgentActions } from "../agent/chartAgent";
import { applyChartAction, applyChartActions } from "../chart/actions";
import { ChartCanvas } from "../chart/ChartCanvas";
import { fetchCandles, fetchSymbols, openChartSocket } from "../chart/cdcClient";
import {
  buildDraftPreviewDrawing,
  buildSingleAnchorPreviewDrawing,
  buildDraggedAnchors,
  defaultDrawingLabel,
  defaultDrawingStyle,
  drawingNeedsTwoAnchors,
  drawingTools,
  drawingTypeFromToolMode,
  hitTestDrawing,
  makeDrawing,
  type DrawingDraft,
  type DrawingDrag
} from "../chart/drawings";
import { expansionCloseButtonSize, expansionMetadataCenterY, expansionParentThumbnailRight } from "../chart/expansionLayout";
import { createCoordinateTransform, hitTestSemanticNode, type ChartScene } from "../chart/scene";
import { adjacentInterval, anchoredViewportForCandles, intervalQueryRangeAround, type IntervalQueryRange, type ViewportAnchor } from "../chart/intervalNavigation";
import {
  candleRange,
  childQueryRange,
  expansionLimitForInterval,
  nextDigTargetInterval,
  semanticNodeId,
  semanticExpansionId,
  snapshotFromSemanticUnit,
  type SemanticExpansion,
  type SemanticRenderUnit,
  type SemanticSelectionSnapshot
} from "../chart/semanticTimeline";
import type { CandleDto, CandleEventDto, ChartAction, ChartInterval, ChartLayerKey, ChartLineExtension, ChartState, ChartSymbolDto, ChartToolMode, DrawingEntity } from "../chart/types";
import { chartIntervals, defaultVisibleBarsForInterval } from "../chart/types";
import { dragDeltaToRightOffset, latestCandleRightOffset, normalizeViewport, zoomViewport, zoomViewportAt, type ChartViewport } from "../chart/viewport";

const initialLayers: Record<ChartLayerKey, boolean> = {
  candles: true,
  volume: true,
  ma5: true,
  ma20: true,
  ma60: true
};

const initialChart: ChartState = {
  symbol: "GOPS-ALP",
  interval: "1D",
  candles: [],
  status: "loading",
  layers: initialLayers,
  volumeRatio: 0.22,
  visibleCount: defaultVisibleBarsForInterval("1D"),
  rightOffset: latestCandleRightOffset(defaultVisibleBarsForInterval("1D")),
  toolMode: "pan",
  trendLineExtension: "segment",
  drawings: [],
  streamState: "connecting"
};

const fallbackSymbols: ChartSymbolDto[] = [
  { symbol: "GOPS-ALP", name: "Alpinary Systems", sector: "Synthetic Cloud Infrastructure", isMock: true },
  { symbol: "GOPS-ION", name: "Ionbridge Dynamics", sector: "Synthetic Energy Platforms", isMock: true },
  { symbol: "GOPS-NOVA", name: "Novastra Fabrication", sector: "Synthetic Robotics", isMock: true }
];

type DragAnchor = {
  x: number;
  y: number;
  rightOffset: number;
  visibleCount: number;
};

type PendingSemanticClick = {
  unit: SemanticRenderUnit;
  x: number;
  y: number;
};

type ExpansionOverlay = {
  id: string;
  label: string;
  left: number;
  right: number;
  top: number;
  status: string;
};

type ChartMemory = {
  expansions: SemanticExpansion[];
  drawings: DrawingEntity[];
  layers: Record<ChartLayerKey, boolean>;
  volumeRatio: number;
  visibleCount: number;
  rightOffset: number;
  selectedDrawingId?: string;
};

type PendingAgentDrawingPreview = {
  id: string;
  actions: ChartAction[];
  drawings: DrawingEntity[];
  visible: boolean;
};

type LiveQuote = {
  priceText: string;
  changeText: string;
  percentText: string;
  tone: "up" | "down" | "flat" | "unavailable";
};

type ChartPanelProps = {
  laneHeight?: number;
  onSemanticSelectionChange?: (selection: SemanticSelectionSnapshot | null) => void;
  onChartHoverChange?: (hovered: boolean) => void;
};

type VolumeResizeDrag = {
  startY: number;
  startRatio: number;
  sceneHeight: number;
};

const priceFormatter = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2
});

const unavailableQuote: LiveQuote = {
  priceText: "-",
  changeText: "-",
  percentText: "-",
  tone: "unavailable"
};

const minLaneHeightForVolume = 245;

export function ChartPanel({ laneHeight, onSemanticSelectionChange, onChartHoverChange }: ChartPanelProps) {
  const [chart, setChart] = useState<ChartState>(initialChart);
  const [symbols, setSymbols] = useState<ChartSymbolDto[]>(fallbackSymbols);
  const [previousClose, setPreviousClose] = useState<number | null>(null);
  const [activeExpansions, setActiveExpansions] = useState<SemanticExpansion[]>([]);
  const [chartMemory, setChartMemory] = useState<Record<string, ChartMemory>>({});
  const [hoveredSemanticNodeId, setHoveredSemanticNodeId] = useState<string | undefined>();
  const [hoverSnapshot, setHoverSnapshot] = useState<SemanticSelectionSnapshot | null>(null);
  const [selectedSemanticNode, setSelectedSemanticNode] = useState<SemanticSelectionSnapshot | null>(null);
  const [expansionOverlays, setExpansionOverlays] = useState<ExpansionOverlay[]>([]);
  const [volumeHandleTop, setVolumeHandleTop] = useState<number | null>(null);
  const [agentInput, setAgentInput] = useState("");
  const [agentMessage, setAgentMessage] = useState("차트 에이전트가 차트 명령을 기다립니다.");
  const [agentBusy, setAgentBusy] = useState(false);
  const [agentDrawingPreview, setAgentDrawingPreview] = useState<PendingAgentDrawingPreview | null>(null);
  const [agentPortalTarget, setAgentPortalTarget] = useState<HTMLElement | null>(null);
  const [symbolSearch, setSymbolSearch] = useState(initialChart.symbol);
  const [symbolSearchOpen, setSymbolSearchOpen] = useState(false);
  const [crosshair, setCrosshair] = useState<{ x: number; y: number } | undefined>();
  const [drawingDraft, setDrawingDraft] = useState<DrawingDraft | null>(null);
  const [maMenuOpen, setMaMenuOpen] = useState(false);
  const [trendMenuOpen, setTrendMenuOpen] = useState(false);
  const [transientViewport, setTransientViewport] = useState<ChartViewport | null>(null);
  const [transientDrawings, setTransientDrawings] = useState<DrawingEntity[] | null>(null);
  const sceneRef = useRef<ChartScene | null>(null);
  const chartRef = useRef<ChartState>(chart);
  const activeExpansionsRef = useRef<SemanticExpansion[]>(activeExpansions);
  const chartMemoryRef = useRef<Record<string, ChartMemory>>(chartMemory);
  const pendingViewportAnchorRef = useRef<{ key: string; anchor: ViewportAnchor; range?: IntervalQueryRange } | null>(null);
  const overlayKeyRef = useRef("");
  const dragAnchorRef = useRef<DragAnchor | null>(null);
  const drawingDragRef = useRef<DrawingDrag | null>(null);
  const pendingSemanticClickRef = useRef<PendingSemanticClick | null>(null);
  const transientViewportRef = useRef<ChartViewport | null>(null);
  const volumeResizeRef = useRef<VolumeResizeDrag | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchSymbols(controller.signal)
      .then((response) => {
        if (response.symbols.length) {
          setSymbols(response.symbols);
          setChart((current) => response.symbols.some((item) => item.symbol === current.symbol)
            ? current
            : applyChartAction(current, { type: "setSymbol", symbol: response.symbols[0].symbol }));
        }
      })
      .catch(() => {
        setSymbols(fallbackSymbols);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    chartRef.current = chart;
  }, [chart]);

  useEffect(() => {
    activeExpansionsRef.current = activeExpansions;
  }, [activeExpansions]);

  useEffect(() => {
    chartMemoryRef.current = chartMemory;
  }, [chartMemory]);

  useEffect(() => {
    const key = chartMemoryKey(chart.symbol, chart.interval);
    const memory = captureChartMemory(chart, activeExpansions);
    setChartMemory((current) => (
      chartMemoryEquals(current[key], memory)
        ? current
        : { ...current, [key]: memory }
    ));
  }, [
    activeExpansions,
    chart.drawings,
    chart.interval,
    chart.layers,
    chart.rightOffset,
    chart.selectedDrawingId,
    chart.symbol,
    chart.visibleCount,
    chart.volumeRatio
  ]);

  useEffect(() => {
    const controller = new AbortController();
    const requestKey = chartMemoryKey(chart.symbol, chart.interval);
    const pendingLoad = pendingViewportAnchorRef.current?.key === requestKey ? pendingViewportAnchorRef.current : null;
    const limit = pendingLoad?.range?.limit ?? defaultVisibleBarsForInterval(chart.interval);
    setChart((current) => ({ ...current, status: "loading", message: "Loading CDC candles..." }));
    fetchCandles({
      symbol: chart.symbol,
      interval: chart.interval,
      limit,
      from: pendingLoad?.range?.from,
      to: pendingLoad?.range?.to,
      ma: [5, 20, 60]
    }, controller.signal)
      .then((response) => {
        setChart((current) => ({
          ...current,
          candles: response.candles,
          status: response.status,
          message: response.error?.message,
          ...anchoredViewportForCandles(
            response.candles,
            current.interval,
            pendingLoad?.anchor ?? null,
            {
              visibleCount: current.visibleCount,
              rightOffset: current.rightOffset
            },
            sceneRef.current ? sceneRef.current.plot.right - sceneRef.current.plot.left : undefined
          )
        }));
        if (pendingViewportAnchorRef.current?.key === requestKey) {
          pendingViewportAnchorRef.current = null;
        }
      })
      .catch((error: unknown) => {
        setChart((current) => ({
          ...current,
          status: "error",
          message: error instanceof Error ? error.message : "Candle request failed"
        }));
      });
    return () => controller.abort();
  }, [chart.symbol, chart.interval]);

  useEffect(() => {
    const controller = new AbortController();
    setPreviousClose(null);
    fetchCandles({ symbol: chart.symbol, interval: "1D", limit: 5, ma: [] }, controller.signal)
      .then((response) => {
        const closed = [...response.candles].reverse().find((candle) => candle.isClosed && Number.isFinite(candle.close));
        setPreviousClose(closed?.close ?? null);
      })
      .catch(() => {
        setPreviousClose(null);
      });
    return () => controller.abort();
  }, [chart.symbol]);

  useEffect(() => {
    return openChartSocket(
      chart.symbol,
      chart.interval,
      (event) => setChart((current) => applyCandleEvent(current, event)),
      (streamState) => setChart((current) => ({ ...current, streamState }))
    );
  }, [chart.symbol, chart.interval]);

  useEffect(() => {
    onSemanticSelectionChange?.(selectedSemanticNode);
  }, [onSemanticSelectionChange, selectedSemanticNode]);

  useEffect(() => {
    if (typeof laneHeight !== "number" || laneHeight >= minLaneHeightForVolume) {
      return;
    }
    setChart((current) => (
      current.layers.volume
        ? applyChartAction(current, { type: "setLayer", layer: "volume", enabled: false })
        : current
    ));
  }, [laneHeight]);

  useEffect(() => {
    setAgentPortalTarget(document.body);
  }, []);

  useEffect(() => {
    const handlePointerMove = (event: PointerEvent) => {
      const drag = volumeResizeRef.current;
      if (!drag) {
        return;
      }
      event.preventDefault();
      const nextRatio = drag.startRatio - (event.clientY - drag.startY) / drag.sceneHeight;
      setChart((current) => applyChartAction(current, { type: "setVolumeRatio", ratio: nextRatio }));
    };
    const handlePointerEnd = () => {
      volumeResizeRef.current = null;
    };
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerEnd);
    window.addEventListener("pointercancel", handlePointerEnd);
    return () => {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerEnd);
      window.removeEventListener("pointercancel", handlePointerEnd);
    };
  }, []);

  const renderChart = useMemo(() => ({
    ...chart,
    visibleCount: transientViewport?.visibleCount ?? chart.visibleCount,
    rightOffset: transientViewport?.rightOffset ?? chart.rightOffset,
    drawings: transientDrawings ?? chart.drawings
  }), [chart, transientDrawings, transientViewport]);
  const renderExpansions = activeExpansions;
  const previewDrawings = useMemo(() => agentDrawingPreview?.visible ? agentDrawingPreview.drawings : [], [agentDrawingPreview]);
  const selectedDrawing = chart.drawings.find((drawing) => drawing.id === chart.selectedDrawingId);
  const currentSymbol = symbols.find((symbol) => symbol.symbol === chart.symbol) ?? fallbackSymbols[0];
  const liveQuote = useMemo(() => buildLiveQuote(chart, previousClose), [chart, previousClose]);
  const currentSymbolLabel = `${currentSymbol.symbol} · ${currentSymbol.name}`;
  const filteredSymbols = useMemo(() => {
    const query = symbolSearch.trim().toLowerCase();
    const matches = query
      ? symbols.filter((symbol) => (
          symbol.symbol.toLowerCase().includes(query) ||
          symbol.name.toLowerCase().includes(query) ||
          symbol.sector?.toLowerCase().includes(query)
        ))
      : symbols;
    return matches.slice(0, 8);
  }, [symbolSearch, symbols]);

  const dispatchChartAction = useCallback((action: ChartAction) => {
    setDrawingDraft(null);
    setTransientDrawings(null);
    setChart((current) => applyChartAction(current, action));
  }, []);

  const persistCurrentChartMemory = useCallback(() => {
    const current = chartRef.current;
    const key = chartMemoryKey(current.symbol, current.interval);
    const memory = captureChartMemory(current, activeExpansionsRef.current);
    chartMemoryRef.current = { ...chartMemoryRef.current, [key]: memory };
    setChartMemory((existing) => (
      chartMemoryEquals(existing[key], memory)
        ? existing
        : { ...existing, [key]: memory }
    ));
  }, []);

  const clearSemanticState = useCallback(() => {
    setHoveredSemanticNodeId(undefined);
    setHoverSnapshot(null);
    setSelectedSemanticNode(null);
    setExpansionOverlays([]);
    setMaMenuOpen(false);
    setTrendMenuOpen(false);
  }, []);

  const switchSymbolInterval = useCallback((
    symbol: string,
    interval: ChartInterval,
    options: { anchor?: ViewportAnchor; range?: IntervalQueryRange | null; expansionOverride?: SemanticExpansion | null } = {}
  ) => {
    persistCurrentChartMemory();
    const normalizedSymbol = symbol.toUpperCase();
    const key = chartMemoryKey(normalizedSymbol, interval);
    const memory = chartMemoryRef.current[key];
    const nextExpansions = Object.prototype.hasOwnProperty.call(options, "expansionOverride")
      ? options.expansionOverride ? [options.expansionOverride] : []
      : memory?.expansions ?? [];
    if (options.anchor) {
      pendingViewportAnchorRef.current = {
        key,
        anchor: {
          ...options.anchor,
          visibleCount: options.anchor.visibleCount ?? memory?.visibleCount ?? defaultVisibleBarsForInterval(interval)
        },
        range: options.range ?? undefined
      };
    } else {
      pendingViewportAnchorRef.current = null;
    }
    activeExpansionsRef.current = nextExpansions;
    setActiveExpansions(nextExpansions);
    setAgentDrawingPreview(null);
    clearSemanticState();
    setChart((current) => restoreChartFromMemory(current, normalizedSymbol, interval, memory));
  }, [clearSemanticState, persistCurrentChartMemory]);

  const setSymbol = useCallback((symbol: string) => {
    switchSymbolInterval(symbol, chartRef.current.interval);
  }, [switchSymbolInterval]);

  const selectSymbol = useCallback((symbol: ChartSymbolDto) => {
    setSymbolSearch(`${symbol.symbol} · ${symbol.name}`);
    setSymbolSearchOpen(false);
    setSymbol(symbol.symbol);
  }, [setSymbol]);

  const setInterval = useCallback((interval: ChartInterval) => {
    const current = chartRef.current;
    if (current.interval === interval) {
      return;
    }
    switchSymbolInterval(current.symbol, interval, {
      anchor: {
        mode: "right",
        timestamp: visibleRightAnchorTimestamp(sceneRef.current, current),
        visibleCount: chartMemoryRef.current[chartMemoryKey(current.symbol, interval)]?.visibleCount ?? defaultVisibleBarsForInterval(interval)
      }
    });
  }, [switchSymbolInterval]);

  const setToolMode = useCallback((toolMode: ChartToolMode) => {
    dispatchChartAction({ type: "setTool", toolMode });
  }, [dispatchChartAction]);

  const setTrendLineExtension = useCallback((extension: ChartLineExtension) => {
    setDrawingDraft(null);
    setTransientDrawings(null);
    setChart((current) => ({ ...current, toolMode: "draw-trendLine", trendLineExtension: extension }));
  }, []);

  const toggleLayer = useCallback((layer: ChartLayerKey) => {
    setDrawingDraft(null);
    setTransientDrawings(null);
    setChart((current) => {
      if (layer === "volume") {
        const enabled = !current.layers.volume;
        if (enabled && typeof laneHeight === "number" && laneHeight < minLaneHeightForVolume) {
          return current;
        }
        return applyChartAction(current, { type: "setLayer", layer, enabled });
      }
      return applyChartAction(current, { type: "toggleLayer", layer });
    });
  }, [laneHeight]);

  const applyAgentActions = useCallback((actions: ChartAction[]) => {
    setDrawingDraft(null);
    setTransientDrawings(null);
    const drawingActions = actions.filter(isDrawingAction);
    const immediateActions = actions.filter((action) => !isDrawingAction(action));
    const baseChart = immediateActions.length ? applyChartActions(chartRef.current, immediateActions) : chartRef.current;
    if (immediateActions.length) {
      setChart(baseChart);
    }
    if (drawingActions.length) {
      const previewChart = applyChartActions(baseChart, drawingActions);
      setAgentDrawingPreview({
        id: `agent-preview-${Date.now()}`,
        actions: drawingActions,
        drawings: changedPreviewDrawings(baseChart.drawings, previewChart.drawings),
        visible: true
      });
    } else {
      setAgentDrawingPreview(null);
    }
  }, []);

  const applyAgentDrawingPreview = useCallback(() => {
    const preview = agentDrawingPreview;
    if (!preview) {
      return;
    }
    setChart((current) => applyChartActions(current, preview.actions));
    setAgentDrawingPreview(null);
  }, [agentDrawingPreview]);

  const applyViewport = useCallback((viewport: ChartViewport) => {
    setChart((current) => {
      const plotWidth = sceneRef.current ? sceneRef.current.plot.right - sceneRef.current.plot.left : undefined;
      const nextViewport = normalizeViewport(viewport, current.candles.length, plotWidth);
      if (nextViewport.visibleCount === current.visibleCount && nextViewport.rightOffset === current.rightOffset) {
        return current;
      }
      return applyChartAction(current, { type: "setViewport", ...nextViewport });
    });
  }, []);

  const handleScene = useCallback((scene: ChartScene) => {
    sceneRef.current = scene;
    const overlays = scene.semantic.expansionRanges.map((range): ExpansionOverlay => ({
      id: range.id,
      label: `${range.childInterval}`,
      left: expansionCloseLeft(scene, range),
      right: range.right,
      top: expansionMetadataCenterY(scene.plot.top) - expansionCloseButtonSize / 2,
      status: range.status
    }));
    const key = JSON.stringify(overlays.map((overlay) => [
      overlay.id,
      Math.round(overlay.left),
      Math.round(overlay.right),
      Math.round(overlay.top),
      overlay.status
    ]));
    if (key !== overlayKeyRef.current) {
      overlayKeyRef.current = key;
      setExpansionOverlays(overlays);
    }
    const nextVolumeHandleTop = scene.chart.layers.volume && scene.plot.volumeTop < scene.plot.bottom ? scene.plot.priceBottom : null;
    setVolumeHandleTop((current) => (
      current === nextVolumeHandleTop ? current : nextVolumeHandleTop
    ));
  }, []);

  const selectSemanticUnit = useCallback((unit: SemanticRenderUnit) => {
    setSelectedSemanticNode(snapshotFromSemanticUnit(unit));
  }, []);

  const closeExpansion = useCallback((expansionId: string) => {
    setActiveExpansions((current) => current.filter((expansion) => expansion.id !== expansionId));
    setSelectedSemanticNode((current) => current?.nodeId.includes(expansionId) ? null : current);
  }, []);

  const loadExpansionCandles = useCallback(async (expansion: SemanticExpansion, symbol: string) => {
    if (expansion.childInterval === "footprint") {
      return;
    }
    const queryRange = childQueryRange({ from: expansion.from, to: expansion.to }, expansion.childInterval);
    try {
      const response = await fetchCandles({
        symbol,
        interval: expansion.childInterval,
        from: queryRange.from,
        to: queryRange.to,
        limit: expansionLimitForInterval(expansion.childInterval),
        ma: [5, 20, 60]
      });
      setActiveExpansions((current) => current.map((item) => (
        item.id === expansion.id &&
        chartRef.current.symbol === symbol
          ? {
              ...item,
              status: response.candles.length ? "ready" : response.status === "error" ? "error" : "empty",
              candles: response.candles,
              message: response.error?.message
            }
          : item
      )));
    } catch (error: unknown) {
      setActiveExpansions((current) => current.map((item) => (
        item.id === expansion.id &&
        chartRef.current.symbol === symbol
          ? {
              ...item,
              status: "error",
              message: error instanceof Error ? error.message : "Expansion candle request failed"
            }
          : item
      )));
    }
  }, []);

  const openSemanticExpansion = useCallback((unit: SemanticRenderUnit) => {
    selectSemanticUnit(unit);
    if (unit.kind !== "candle") {
      return;
    }
    const rootParentNodeId = unit.parentExpansionId
      ? semanticNodeId(unit.symbol, unit.interval, unit.timestamp)
      : unit.id;
    const expansion = buildSemanticExpansion(unit, rootParentNodeId, unit.parentExpansionId ? 1 : unit.depth + 1);
    if (unit.parentExpansionId) {
      const visibleCount = chartMemoryRef.current[chartMemoryKey(unit.symbol, unit.interval)]?.visibleCount ?? defaultVisibleBarsForInterval(unit.interval);
      switchSymbolInterval(unit.symbol, unit.interval, {
        anchor: {
          mode: "center",
          timestamp: unit.timestamp,
          visibleCount
        },
        range: intervalQueryRangeAround(unit.timestamp, unit.interval, visibleCount),
        expansionOverride: expansion
      });
    } else {
      activeExpansionsRef.current = upsertExpansion(activeExpansionsRef.current, expansion);
      setActiveExpansions((current) => upsertExpansion(current, expansion));
    }
    if (expansion.childInterval === "footprint") {
      setSelectedSemanticNode({
        ...snapshotFromSemanticUnit(unit),
        status: "ready"
      });
      return;
    }
    void loadExpansionCandles(expansion, unit.symbol);
  }, [loadExpansionCandles, selectSemanticUnit, switchSymbolInterval]);

  const zoomBy = useCallback((delta: number) => {
    setChart((current) => {
      const plotWidth = sceneRef.current ? sceneRef.current.plot.right - sceneRef.current.plot.left : undefined;
      const nextViewport = zoomViewport(
        { visibleCount: current.visibleCount, rightOffset: current.rightOffset },
        delta,
        current.candles.length,
        plotWidth
      );
      return applyChartAction(current, { type: "setViewport", ...nextViewport });
    });
  }, []);

  const runAgent = async (event: FormEvent) => {
    event.preventDefault();
    const prompt = agentInput.trim();
    if (!prompt || agentBusy) {
      return;
    }
    setAgentInput("");
    setAgentBusy(true);
    setAgentMessage("차트 에이전트가 차트를 읽고 있습니다.");
    try {
      const result = await requestChartAgentActions({ prompt, chart });
      setAgentMessage(result.message);
      if (result.actions.length) {
        applyAgentActions(result.actions);
      }
    } catch (error: unknown) {
      setAgentMessage(error instanceof Error ? error.message : "차트 에이전트 요청에 실패했습니다.");
    } finally {
      setAgentBusy(false);
    }
  };

  const maLayerButtons = useMemo(() => ([
    ["ma5", "MA5"],
    ["ma20", "MA20"],
    ["ma60", "MA60"]
  ] as Array<[ChartLayerKey, string]>), []);
  const trendExtensionButtons = useMemo(() => ([
    ["segment", "Segment"],
    ["ray", "Ray"],
    ["line", "Line"]
  ] as Array<[ChartLineExtension, string]>), []);
  const anyMaEnabled = chart.layers.ma5 || chart.layers.ma20 || chart.layers.ma60;

  const handleWheel = (event: ReactWheelEvent<HTMLCanvasElement>) => {
    event.preventDefault();
    onChartHoverChange?.(true);
    const step = Math.max(12, Math.round(chart.visibleCount * 0.12));
    const delta = event.deltaY > 0 ? step : -step;
    const scene = sceneRef.current;
    if (!scene) {
      zoomBy(delta);
      return;
    }
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const plotWidth = Math.max(1, scene.plot.right - scene.plot.left);
    const anchorRatio = (x - scene.plot.left) / plotWidth;
    setChart((current) => {
      const nextViewport = zoomViewportAt(
        { visibleCount: current.visibleCount, rightOffset: current.rightOffset },
        delta,
        current.candles.length,
        anchorRatio,
        plotWidth
      );
      return applyChartAction(current, { type: "setViewport", ...nextViewport });
    });
  };

  const handlePointerDown = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    onChartHoverChange?.(true);
    if (event.button !== 0) {
      return;
    }
    setMaMenuOpen(false);
    setTrendMenuOpen(false);
    const scene = sceneRef.current;
    if (!scene) {
      return;
    }
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const rect = event.currentTarget.getBoundingClientRect();
    const point = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    const transform = createCoordinateTransform(scene);
    const semanticHit = hitTestSemanticNode(scene, point.x, point.y);

    if (chart.toolMode === "select") {
      const hit = hitTestDrawing(scene, point.x, point.y);
      if (!hit) {
        if (semanticHit) {
          pendingSemanticClickRef.current = { unit: semanticHit, x: event.clientX, y: event.clientY };
          selectSemanticUnit(semanticHit);
          return;
        }
        dispatchChartAction({ type: "selectDrawing" });
        return;
      }
      const anchor = transform.pointToAnchor(point.x, point.y, chart.symbol);
      if (anchor) {
        drawingDragRef.current = { drawing: hit.drawing, anchor, anchorIndex: hit.anchorIndex };
      }
      dispatchChartAction({ type: "selectDrawing", drawingId: hit.drawing.id });
      return;
    }

    const drawingType = drawingTypeFromToolMode(chart.toolMode);
    if (drawingType) {
      const anchor = transform.pointToAnchor(point.x, point.y, chart.symbol);
      if (!anchor) {
        return;
      }
      if (!drawingNeedsTwoAnchors(drawingType)) {
        const drawing = makeDrawing(drawingType, [anchor], { trendLineExtension: chart.trendLineExtension });
        dispatchChartAction({ type: "addDrawing", drawing });
        return;
      }
      if (drawingDraft?.type === drawingType) {
        const drawing = makeDrawing(drawingType, [drawingDraft.first, anchor], { trendLineExtension: chart.trendLineExtension });
        setDrawingDraft(null);
        setTransientDrawings(null);
        dispatchChartAction({ type: "addDrawing", drawing });
      } else {
        setDrawingDraft({ type: drawingType, first: anchor });
        setTransientDrawings(null);
      }
      return;
    }

    if (semanticHit) {
      pendingSemanticClickRef.current = { unit: semanticHit, x: event.clientX, y: event.clientY };
    }
    const currentViewport = normalizeViewport({ visibleCount: chart.visibleCount, rightOffset: chart.rightOffset }, chart.candles.length, scene.plot.right - scene.plot.left);
    dragAnchorRef.current = {
      x: event.clientX,
      y: event.clientY,
      rightOffset: currentViewport.rightOffset,
      visibleCount: currentViewport.visibleCount
    };
    transientViewportRef.current = currentViewport;
    setTransientViewport(currentViewport);
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    onChartHoverChange?.(true);
    const rect = event.currentTarget.getBoundingClientRect();
    const point = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    setCrosshair(point);
    const scene = sceneRef.current;
    if (!scene) {
      return;
    }
    const semanticHit = hitTestSemanticNode(scene, point.x, point.y);
    setHoveredSemanticNodeId(semanticHit?.id);
    setHoverSnapshot(semanticHit ? snapshotFromSemanticUnit(semanticHit) : null);

    const drawingDrag = drawingDragRef.current;
    if (drawingDrag) {
      const anchor = createCoordinateTransform(scene).pointToAnchor(point.x, point.y, chart.symbol);
      if (!anchor) {
        return;
      }
      const anchors = buildDraggedAnchors(drawingDrag, anchor, scene);
      setTransientDrawings(chart.drawings.map((drawing) => (
        drawing.id === drawingDrag.drawing.id ? { ...drawing, anchors, updatedAt: new Date().toISOString() } : drawing
      )));
      return;
    }

    const activeDrawingType = drawingTypeFromToolMode(chart.toolMode);
    if (activeDrawingType && !drawingNeedsTwoAnchors(activeDrawingType)) {
      const anchor = createCoordinateTransform(scene).pointToAnchor(point.x, point.y, chart.symbol);
      if (!anchor) {
        setTransientDrawings(null);
        return;
      }
      setTransientDrawings([
        ...chart.drawings,
        buildSingleAnchorPreviewDrawing(activeDrawingType, anchor, chart.trendLineExtension)
      ]);
      return;
    }

    if (drawingDraft && chart.toolMode === `draw-${drawingDraft.type}`) {
      const anchor = createCoordinateTransform(scene).pointToAnchor(point.x, point.y, chart.symbol);
      if (!anchor) {
        setTransientDrawings(null);
        return;
      }
      setTransientDrawings([
        ...chart.drawings,
        buildDraftPreviewDrawing(drawingDraft, anchor, chart.trendLineExtension)
      ]);
      return;
    }

    const dragAnchor = dragAnchorRef.current;
    if (!dragAnchor) {
      return;
    }
    const nextViewport = {
      visibleCount: dragAnchor.visibleCount,
      rightOffset: dragDeltaToRightOffset(
        dragAnchor.rightOffset,
        event.clientX - dragAnchor.x,
        scene.scales.slotWidth,
        dragAnchor.visibleCount,
        chart.candles.length
      )
    };
    transientViewportRef.current = nextViewport;
    setTransientViewport(nextViewport);
  };

  const handlePointerUp = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const drawingDrag = drawingDragRef.current;
    const dragAnchor = dragAnchorRef.current;
    const pendingSemanticClick = pendingSemanticClickRef.current;
    const nextViewport = transientViewportRef.current;
    drawingDragRef.current = null;
    dragAnchorRef.current = null;
    pendingSemanticClickRef.current = null;
    transientViewportRef.current = null;
    setTransientViewport(null);
    setTransientDrawings(null);
    try {
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    } catch {
      // Pointer capture can be released by the browser before this handler runs.
    }
    if (drawingDrag) {
      const scene = sceneRef.current;
      const rect = event.currentTarget.getBoundingClientRect();
      const point = { x: event.clientX - rect.left, y: event.clientY - rect.top };
      const anchor = scene ? createCoordinateTransform(scene).pointToAnchor(point.x, point.y, chart.symbol) : null;
      if (scene && anchor) {
        const anchors = buildDraggedAnchors(drawingDrag, anchor, scene);
        dispatchChartAction({ type: "updateDrawing", drawingId: drawingDrag.drawing.id, patch: { anchors } });
      }
      return;
    }
    if (pendingSemanticClick) {
      const distance = Math.hypot(event.clientX - pendingSemanticClick.x, event.clientY - pendingSemanticClick.y);
      if (distance <= 5) {
        void openSemanticExpansion(pendingSemanticClick.unit);
        return;
      }
    }
    if (dragAnchor && nextViewport && nextViewport.rightOffset !== dragAnchor.rightOffset) {
      applyViewport(nextViewport);
    }
  };

  const cancelDrag = () => {
    drawingDragRef.current = null;
    dragAnchorRef.current = null;
    pendingSemanticClickRef.current = null;
    transientViewportRef.current = null;
    setTransientViewport(null);
    setTransientDrawings(null);
    setHoveredSemanticNodeId(undefined);
    setHoverSnapshot(null);
    setCrosshair(undefined);
    onChartHoverChange?.(false);
  };

  const clearChartHover = useCallback(() => {
    if (dragAnchorRef.current || drawingDragRef.current) {
      return;
    }
    setHoveredSemanticNodeId(undefined);
    setHoverSnapshot(null);
    setCrosshair(undefined);
    onChartHoverChange?.(false);
  }, [onChartHoverChange]);

  useEffect(() => {
    if (!symbolSearchOpen) {
      setSymbolSearch(currentSymbolLabel);
    }
  }, [currentSymbolLabel, symbolSearchOpen]);

  const removeSelectedDrawing = () => {
    if (!selectedDrawing) {
      return;
    }
    dispatchChartAction({ type: "deleteDrawing", drawingId: selectedDrawing.id });
  };

  const updateSelectedDrawingStyle = () => {
    if (!selectedDrawing) {
      return;
    }
    const nextColor = selectedDrawing.style.color === "#dc2626" ? defaultDrawingStyle(selectedDrawing.type, chart.trendLineExtension).color : "#dc2626";
    dispatchChartAction({
      type: "updateDrawing",
      drawingId: selectedDrawing.id,
      patch: { style: { ...selectedDrawing.style, color: nextColor, textColor: nextColor } }
    });
  };

  const clearAllDrawings = () => {
    setDrawingDraft(null);
    setTransientDrawings(null);
    dispatchChartAction({ type: "clearDrawings" });
  };

  const beginVolumeResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    const scene = sceneRef.current;
    if (!scene || !chart.layers.volume) {
      return;
    }
    event.currentTarget.setPointerCapture?.(event.pointerId);
    onChartHoverChange?.(true);
    volumeResizeRef.current = {
      startY: event.clientY,
      startRatio: chart.volumeRatio,
      sceneHeight: Math.max(1, scene.height)
    };
  };

  const updateVolumeResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = volumeResizeRef.current;
    if (!drag) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    const nextRatio = drag.startRatio - (event.clientY - drag.startY) / drag.sceneHeight;
    setChart((current) => applyChartAction(current, { type: "setVolumeRatio", ratio: nextRatio }));
  };

  const endVolumeResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    volumeResizeRef.current = null;
    try {
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    } catch {
      // Pointer capture can already be released after a resize drag.
    }
  };

  const smallerInterval = adjacentInterval(chart.interval, "smaller");
  const largerInterval = adjacentInterval(chart.interval, "larger");
  const hasAnyCurrentSymbolExpansion = activeExpansions.length > 0 || Object.entries(chartMemory).some(([key, memory]) => (
    key.startsWith(`${chart.symbol}:`) && memory.expansions.length > 0
  ));

  const clearAllDigging = () => {
    activeExpansionsRef.current = [];
    setActiveExpansions([]);
    setSelectedSemanticNode(null);
    setExpansionOverlays([]);
    setChartMemory((current) => {
      const next = { ...current };
      Object.entries(next).forEach(([key, memory]) => {
        if (key.startsWith(`${chart.symbol}:`) && memory.expansions.length > 0) {
          next[key] = { ...memory, expansions: [] };
        }
      });
      chartMemoryRef.current = next;
      return next;
    });
  };

  return (
    <section className="chart-panel">
      <header className="panel-header">
        <div>
          <h1>{chart.symbol} <span>{chart.interval}</span></h1>
          <p className="symbol-name">{currentSymbol.name}</p>
        </div>
        <div className={`live-quote ${liveQuote.tone}`} aria-label="Live quote">
          <span className="quote-price">{liveQuote.priceText}</span>
          <span className="quote-change">{liveQuote.changeText}</span>
          <span className="quote-percent">{liveQuote.percentText}</span>
        </div>
      </header>
      {hoverSnapshot?.kind === "candle" && (
        <dl className="hover-ohlc hover-ohlc-overlay" aria-label="Hovered candle data">
          <div className="hover-ohlc-time"><dt>Time</dt><dd>{formatHoverTimestamp(hoverSnapshot.timestamp ?? hoverSnapshot.from)}</dd></div>
          <div><dt>O</dt><dd>{formatMetric(hoverSnapshot.open)}</dd></div>
          <div><dt>C</dt><dd>{formatMetric(hoverSnapshot.close)}</dd></div>
          <div><dt>H</dt><dd>{formatMetric(hoverSnapshot.high)}</dd></div>
          <div><dt>L</dt><dd>{formatMetric(hoverSnapshot.low)}</dd></div>
        </dl>
      )}

      <div className="toolbar" aria-label="Chart controls">
        <div className="toolbar-row">
          <div className="interval-stepper" aria-label="Interval controls">
            <button type="button" className="icon-button" aria-label="Smaller interval" title="Smaller interval" disabled={!smallerInterval} onClick={() => smallerInterval && setInterval(smallerInterval)}>
              <ChevronDown size={15} />
            </button>
            <select value={chart.interval} onChange={(event) => setInterval(event.target.value as ChartInterval)} aria-label="Interval">
              {chartIntervals.map((interval) => <option key={interval} value={interval}>{interval}</option>)}
            </select>
            <button type="button" className="icon-button" aria-label="Larger interval" title="Larger interval" disabled={!largerInterval} onClick={() => largerInterval && setInterval(largerInterval)}>
              <ChevronUp size={15} />
            </button>
          </div>
          <button className={chart.layers.volume ? "segmented active" : "segmented"} onClick={() => toggleLayer("volume")} type="button">
            VOL
          </button>
          <button className="segmented" disabled={!hasAnyCurrentSymbolExpansion} onClick={clearAllDigging} type="button" title="Clear all digging">
            DIG OFF
          </button>
          <div className="ma-control">
            <button
              type="button"
              className={anyMaEnabled ? "segmented active" : "segmented"}
              aria-expanded={maMenuOpen}
              onClick={() => {
                setTrendMenuOpen(false);
                setMaMenuOpen((current) => !current);
              }}
            >
              MA
            </button>
            {maMenuOpen && (
              <div className="ma-menu" role="menu" aria-label="Moving averages">
                {maLayerButtons.map(([layer, label]) => (
                  <button
                    key={layer}
                    type="button"
                    className={chart.layers[layer] ? "segmented active" : "segmented"}
                    onClick={() => toggleLayer(layer)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            )}
          </div>
          <span className="toolbar-separator" aria-hidden="true" />
          {drawingTools.map((tool) => (
            tool.mode === "draw-trendLine" ? (
              <div className="trend-control" key={tool.mode}>
                <button
                  type="button"
                  className={chart.toolMode === tool.mode ? "icon-button active" : "icon-button"}
                  aria-label={tool.label}
                  title={tool.label}
                  aria-expanded={trendMenuOpen}
                  onClick={() => {
                    setMaMenuOpen(false);
                    setToolMode(tool.mode);
                    setTrendMenuOpen((current) => chart.toolMode === tool.mode ? !current : true);
                  }}
                >
                  <ToolIcon toolMode={tool.mode} />
                </button>
                {trendMenuOpen && (
                  <div className="trend-menu" role="menu" aria-label="Trend line type">
                    {trendExtensionButtons.map(([extension, label]) => (
                      <button
                        key={extension}
                        type="button"
                        className={chart.trendLineExtension === extension ? "icon-button active" : "icon-button"}
                        aria-label={label}
                        title={label}
                        onClick={() => setTrendLineExtension(extension)}
                      >
                        <TrendExtensionIcon extension={extension} />
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <button
                key={tool.mode}
                type="button"
                className={chart.toolMode === tool.mode ? "icon-button active" : "icon-button"}
                aria-label={tool.label}
                title={tool.label}
                onClick={() => {
                  setTrendMenuOpen(false);
                  setToolMode(tool.mode);
                }}
              >
                <ToolIcon toolMode={tool.mode} />
              </button>
            )
          ))}
          <button type="button" className="icon-button" aria-label="Selected drawing color" title="Selected drawing color" disabled={!selectedDrawing} onClick={updateSelectedDrawingStyle}>
            <Palette size={16} />
          </button>
          <button type="button" className="icon-button" aria-label="Delete selected drawing" title="Delete selected drawing" disabled={!selectedDrawing} onClick={removeSelectedDrawing}>
            <Eraser size={16} />
          </button>
          <button type="button" className="icon-button" aria-label="Clear drawings" title="Clear drawings" disabled={chart.drawings.length === 0} onClick={clearAllDrawings}>
            <Trash2 size={16} />
          </button>
          {drawingDraft && <span className="draft-pill">{defaultDrawingLabel(drawingDraft.type) ?? drawingDraft.type} 2nd point</span>}
        </div>
      </div>

      <div className="chart-wrap">
        <ChartCanvas
          chart={renderChart}
          expansions={renderExpansions}
          previewDrawings={previewDrawings}
          hoveredNodeId={hoveredSemanticNodeId}
          selectedNodeId={selectedSemanticNode?.nodeId}
          crosshair={crosshair}
          onScene={handleScene}
          onWheel={handleWheel}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerLeave={() => {
            setHoveredSemanticNodeId(undefined);
            setHoverSnapshot(null);
            if (!dragAnchorRef.current && !drawingDragRef.current) {
              onChartHoverChange?.(false);
            }
            if (!dragAnchorRef.current) {
              setCrosshair(undefined);
            }
            if (!dragAnchorRef.current && !drawingDragRef.current) {
              setTransientDrawings(null);
            }
          }}
          onPointerUp={handlePointerUp}
          onPointerCancel={cancelDrag}
          onLostPointerCapture={cancelDrag}
        />
        {volumeHandleTop !== null && (
          <div
            className="volume-resize-handle"
            style={{ top: volumeHandleTop - 5 }}
            aria-label="Resize volume chart"
            role="separator"
            onPointerDown={beginVolumeResize}
            onPointerMove={updateVolumeResize}
            onPointerUp={endVolumeResize}
            onPointerCancel={endVolumeResize}
          />
        )}
        {expansionOverlays.map((overlay) => (
          <button
            key={overlay.id}
            type="button"
            className={`semantic-expansion-close ${overlay.status}`}
            style={{ left: overlay.left, top: overlay.top }}
            aria-label={`Close ${overlay.label} expansion`}
            title={`Close ${overlay.label} expansion`}
            onClick={() => closeExpansion(overlay.id)}
          >
            <X size={13} />
          </button>
        ))}
      </div>

      {agentPortalTarget && createPortal(
        <div className="agent-overlay" onPointerEnter={(event) => event.stopPropagation()} onPointerMove={(event) => event.stopPropagation()} onPointerLeave={(event) => event.stopPropagation()}>
          <div className="symbol-search" onPointerEnter={clearChartHover} onPointerMove={clearChartHover}>
            <input
              value={symbolSearch}
              onChange={(event) => {
                setSymbolSearch(event.target.value);
                setSymbolSearchOpen(true);
              }}
              onFocus={() => {
                setSymbolSearchOpen(true);
                setSymbolSearch("");
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" && filteredSymbols[0]) {
                  event.preventDefault();
                  selectSymbol(filteredSymbols[0]);
                }
                if (event.key === "Escape") {
                  setSymbolSearchOpen(false);
                  setSymbolSearch(currentSymbolLabel);
                }
              }}
              placeholder="종목 검색"
              aria-label="Symbol search"
              autoComplete="off"
            />
            <button
              type="button"
              aria-label="Open symbol dropdown"
              title="Open symbol dropdown"
              onClick={() => {
                if (symbolSearchOpen) {
                  setSymbolSearchOpen(false);
                  setSymbolSearch(currentSymbolLabel);
                  return;
                }
                setSymbolSearch("");
                setSymbolSearchOpen(true);
              }}
            >
              {chart.symbol}
            </button>
            {symbolSearchOpen && (
              <div className="symbol-search-menu" role="listbox" aria-label="Symbols">
                {filteredSymbols.map((symbol) => (
                  <button
                    key={symbol.symbol}
                    type="button"
                    className={symbol.symbol === chart.symbol ? "active" : ""}
                    role="option"
                    aria-selected={symbol.symbol === chart.symbol}
                    onPointerDown={(event) => {
                      event.preventDefault();
                      selectSymbol(symbol);
                    }}
                  >
                    <strong>{symbol.symbol}</strong>
                    <span>{symbol.name}</span>
                  </button>
                ))}
                {!filteredSymbols.length && <p>검색 결과 없음</p>}
              </div>
            )}
          </div>
          {agentMessage && agentMessage !== "차트 에이전트가 차트 명령을 기다립니다." && (
            <p className="agent-answer">{agentMessage}</p>
          )}
          {agentDrawingPreview && (
            <div className="agent-preview-controls" onPointerEnter={clearChartHover} onPointerMove={clearChartHover}>
              <button
                type="button"
                className={agentDrawingPreview.visible ? "active" : ""}
                aria-label={agentDrawingPreview.visible ? "Hide agent drawing preview" : "Show agent drawing preview"}
                title={agentDrawingPreview.visible ? "Hide preview" : "Show preview"}
                onClick={() => setAgentDrawingPreview((current) => current ? { ...current, visible: !current.visible } : current)}
              >
                {agentDrawingPreview.visible ? <EyeOff size={14} /> : <Eye size={14} />}
                Preview
              </button>
              <button type="button" aria-label="Apply agent drawing preview" title="Apply preview" onClick={applyAgentDrawingPreview}>
                <Check size={14} />
                Apply
              </button>
              <button type="button" aria-label="Discard agent drawing preview" title="Discard preview" onClick={() => setAgentDrawingPreview(null)}>
                <X size={14} />
                Discard
              </button>
            </div>
          )}
          <form className="agent-box" onPointerEnter={clearChartHover} onPointerMove={clearChartHover} onSubmit={runAgent}>
            <input
              value={agentInput}
              onChange={(event) => setAgentInput(event.target.value)}
              placeholder="차트에게 물어보기"
              aria-label="Chart agent command"
              disabled={agentBusy}
            />
            <button type="submit" disabled={agentBusy}>{agentBusy ? "..." : "Run"}</button>
          </form>
        </div>,
        agentPortalTarget
      )}
    </section>
  );
}

function chartMemoryKey(symbol: string, interval: ChartInterval): string {
  return `${symbol.toUpperCase()}:${interval}`;
}

function expansionCloseLeft(scene: ChartScene, range: ChartScene["semantic"]["expansionRanges"][number]): number {
  const rightInset = 8;
  const thumbnailGap = 8;
  const desiredLeft = range.right - expansionCloseButtonSize - rightInset;
  const maxVisibleLeft = scene.plot.right - expansionCloseButtonSize - rightInset;
  const thumbnailStopLeft = expansionParentThumbnailRight(scene.plot, range) + thumbnailGap;

  if (desiredLeft > maxVisibleLeft) {
    return maxVisibleLeft >= thumbnailStopLeft ? maxVisibleLeft : desiredLeft;
  }
  return Math.max(desiredLeft, thumbnailStopLeft);
}

function upsertExpansion(expansions: SemanticExpansion[], expansion: SemanticExpansion): SemanticExpansion[] {
  const index = expansions.findIndex((item) => item.id === expansion.id);
  if (index < 0) {
    return [...expansions, expansion];
  }
  const next = [...expansions];
  next[index] = expansion;
  return next;
}

function captureChartMemory(chart: ChartState, expansions: SemanticExpansion[]): ChartMemory {
  return {
    expansions,
    drawings: chart.drawings,
    layers: { ...chart.layers },
    volumeRatio: chart.volumeRatio,
    visibleCount: chart.visibleCount,
    rightOffset: chart.rightOffset,
    selectedDrawingId: chart.selectedDrawingId
  };
}

function chartMemoryEquals(left: ChartMemory | undefined, right: ChartMemory): boolean {
  if (!left) {
    return false;
  }
  return (
    sameExpansionList(left.expansions, right.expansions) &&
    left.drawings === right.drawings &&
    left.volumeRatio === right.volumeRatio &&
    left.visibleCount === right.visibleCount &&
    left.rightOffset === right.rightOffset &&
    left.selectedDrawingId === right.selectedDrawingId &&
    left.layers.candles === right.layers.candles &&
    left.layers.volume === right.layers.volume &&
    left.layers.ma5 === right.layers.ma5 &&
    left.layers.ma20 === right.layers.ma20 &&
    left.layers.ma60 === right.layers.ma60
  );
}

function sameExpansionList(left: SemanticExpansion[], right: SemanticExpansion[]): boolean {
  return left.length === right.length && left.every((item, index) => item === right[index]);
}

function restoreChartFromMemory(
  current: ChartState,
  symbol: string,
  interval: ChartInterval,
  memory: ChartMemory | undefined
): ChartState {
  const sameDataKey = current.symbol === symbol && current.interval === interval;
  return {
    ...current,
    symbol,
    interval,
    candles: sameDataKey ? current.candles : [],
    status: "loading",
    message: "Loading CDC candles...",
    layers: memory?.layers ? { ...memory.layers } : { ...initialLayers },
    volumeRatio: memory?.volumeRatio ?? initialChart.volumeRatio,
    visibleCount: memory?.visibleCount ?? defaultVisibleBarsForInterval(interval),
    rightOffset: memory?.rightOffset ?? latestCandleRightOffset(defaultVisibleBarsForInterval(interval)),
    drawings: memory?.drawings ?? [],
    selectedDrawingId: memory?.selectedDrawingId,
    streamState: sameDataKey ? current.streamState : "connecting"
  };
}

function visibleRightAnchorTimestamp(scene: ChartScene | null, chart: ChartState): string | undefined {
  if (scene && scene.chart.symbol === chart.symbol && scene.chart.interval === chart.interval) {
    const index = Math.max(0, scene.visibleEndIndex - 1);
    return scene.allCandles[index]?.timestamp ?? chart.candles.at(-1)?.timestamp;
  }
  return chart.candles.at(-1)?.timestamp;
}

function buildSemanticExpansion(unit: Extract<SemanticRenderUnit, { kind: "candle" }>, parentNodeId: string, depth: number): SemanticExpansion {
  const childInterval = nextDigTargetInterval(unit.interval);
  const range = candleRange(unit.candle, unit.interval);
  return {
    id: semanticExpansionId(parentNodeId),
    parentNodeId,
    parentTimestamp: unit.timestamp,
    parentInterval: unit.interval,
    parentCandle: unit.candle,
    childInterval,
    from: range.from,
    to: range.to,
    depth,
    status: childInterval === "footprint" ? "ready" : "loading",
    candles: [],
    openedAt: new Date().toISOString()
  };
}

function isDrawingAction(action: ChartAction): boolean {
  return action.type === "addDrawing" ||
    action.type === "updateDrawing" ||
    action.type === "deleteDrawing" ||
    action.type === "clearDrawings";
}

function changedPreviewDrawings(baseDrawings: DrawingEntity[], previewDrawings: DrawingEntity[]): DrawingEntity[] {
  return previewDrawings.filter((drawing) => {
    const base = baseDrawings.find((item) => item.id === drawing.id);
    return !base || JSON.stringify(base) !== JSON.stringify(drawing);
  });
}

function buildLiveQuote(chart: ChartState, previousClose: number | null): LiveQuote {
  if (chart.streamState !== "live" || !previousClose || previousClose <= 0) {
    return unavailableQuote;
  }
  const latest = chart.candles.at(-1);
  if (!latest || !Number.isFinite(latest.close)) {
    return unavailableQuote;
  }
  const change = latest.close - previousClose;
  const percent = (change / previousClose) * 100;
  const tone = change > 0 ? "up" : change < 0 ? "down" : "flat";
  return {
    priceText: priceFormatter.format(latest.close),
    changeText: formatSignedNumber(change),
    percentText: `${formatSignedNumber(percent)}%`,
    tone
  };
}

function formatSignedNumber(value: number): string {
  if (!Number.isFinite(value)) {
    return "-";
  }
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${sign}${priceFormatter.format(Math.abs(value))}`;
}

function formatMetric(value: number | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "-";
}

function formatHoverTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}

function TrendExtensionIcon({ extension }: { extension: ChartLineExtension }) {
  switch (extension) {
    case "segment":
      return (
        <svg className="trend-extension-icon" viewBox="0 0 18 18" aria-hidden="true" focusable="false">
          <line x1="4" y1="14" x2="14" y2="4" />
          <circle cx="4" cy="14" r="1.8" />
          <circle cx="14" cy="4" r="1.8" />
        </svg>
      );
    case "ray":
      return (
        <svg className="trend-extension-icon" viewBox="0 0 18 18" aria-hidden="true" focusable="false">
          <line x1="4" y1="14" x2="14" y2="4" />
          <circle cx="4" cy="14" r="1.8" />
          <polyline points="10 4 14 4 14 8" />
        </svg>
      );
    case "line":
      return (
        <svg className="trend-extension-icon" viewBox="0 0 18 18" aria-hidden="true" focusable="false">
          <line x1="4" y1="14" x2="14" y2="4" />
          <polyline points="10 4 14 4 14 8" />
          <polyline points="8 14 4 14 4 10" />
        </svg>
      );
  }
}

function ToolIcon({ toolMode }: { toolMode: ChartToolMode }) {
  switch (toolMode) {
    case "select":
      return <MousePointer2 size={16} />;
    case "pan":
      return <Hand size={16} />;
    case "draw-horizontalLine":
      return <Minus size={16} />;
    case "draw-trendLine":
      return <span className="tool-glyph diagonal-line" aria-hidden="true" />;
    case "draw-verticalMarker":
      return <span className="tool-glyph vertical-line" aria-hidden="true" />;
    case "draw-textLabel":
      return <Type size={16} />;
    case "draw-pointMarker":
      return <CircleDot size={16} />;
    case "draw-arrow":
      return <ArrowUpRight size={16} />;
    case "draw-rangeBox":
      return <Square size={16} />;
    case "draw-measurement":
      return <Ruler size={16} />;
    default:
      return <MousePointer2 size={16} />;
  }
}

function applyCandleEvent(chart: ChartState, event: CandleEventDto): ChartState {
  if (event.symbol !== chart.symbol || event.interval !== chart.interval) {
    return chart;
  }
  const timestamp = event.data.timestamp;
  const nextCandle = { ...event.data };
  const candles = [...chart.candles].sort(compareCandles);
  const index = candles.findIndex((candle) => candle.timestamp === timestamp);
  if (index >= 0) {
    candles[index] = nextCandle;
    return { ...chart, candles: candles.sort(compareCandles), status: "ready" };
  }
  const latest = candles.at(-1);
  if (latest && Date.parse(timestamp) < Date.parse(latest.timestamp)) {
    return chart;
  }
  candles.push(nextCandle);
  return { ...chart, candles: candles.sort(compareCandles), status: "ready" };
}

function compareCandles(left: CandleDto, right: CandleDto): number {
  return Date.parse(left.timestamp) - Date.parse(right.timestamp);
}
