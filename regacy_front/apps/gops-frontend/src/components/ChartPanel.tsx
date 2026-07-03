import {
  ArrowUpRight,
  BarChart3,
  Bot,
  ChartCandlestick,
  ChartLine,
  Check,
  CircleDot,
  Eraser,
  Eye,
  EyeOff,
  Hand,
  Minus,
  MoveLeft,
  MoveRight,
  MousePointer2,
  Palette,
  RefreshCcw,
  RotateCcw,
  RotateCw,
  Ruler,
  Square,
  TextCursor,
  Trash2,
  Type,
  ZoomIn,
  ZoomOut
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, WheelEvent as ReactWheelEvent } from "react";
import { createPortal } from "react-dom";
import {
  isActiveBackfillStatus,
  isChartDataRenderable,
  isPreparingCandleData,
  normalizeBackfillStatusPayload,
  historicalRangeReadRequest,
  planHistoricalRangeLoad,
  rangeBackfillWindow,
  shouldRequestBackfill,
  shouldRequestRangeBackfill
} from "@gops/chart-engine/backfill";
import { drawChartScene } from "@gops/chart-engine/canvasRenderer";
import { makeChartCommand } from "@gops/chart-engine/commands";
import { normalizeLineExtension, projectTrendLine } from "@gops/chart-engine/drawingGeometry";
import { chartToolRegistry, drawingNeedsTwoAnchors } from "@gops/chart-engine/registries";
import {
  chartIntervals,
  defaultVisibleBarsForInterval,
  minimumBackfillSourceBarsForInterval,
  rangeBackfillBufferMultiplierForInterval
} from "@gops/chart-engine/intervals";
import { isRealtimeControlPayload, normalizeCandleEvent, normalizeCandleSnapshot } from "@gops/chart-engine/marketDataAdapter";
import { buildRenderScene } from "@gops/chart-engine/renderScene";
import { createCoordinateTransform } from "@gops/chart-engine/scales";
import { clampRightOffset, dragDeltaToRightOffset, normalizeViewport, zoomViewport } from "@gops/chart-engine/viewport";
import {
  getCandlesForDocument,
  getChartDocumentForPanel,
  getDataStatusForDocument,
  getStreamMessageForDocument,
  getStreamStatusForDocument,
  type ChartRuntimeAction,
  type ChartRuntimeState
} from "@gops/chart-engine/runtime";
import { candleKey } from "@gops/chart-engine/candleStore";
import { normalizeSupportedSymbol, normalizeWatchlistPayload, type SupportedSymbol } from "@gops/chart-engine/symbols";
import type { ChartLayerKey, ChartLineExtension, ChartToolMode, DrawingAnchor, DrawingEntity, DrawingType, ChartViewport, RenderScene, StreamStatus } from "@gops/chart-engine/types";
import { summarizeChartCoverage, type ChartDevLogInput } from "../diagnostics/chartDevLog";
import { useElementSize } from "../hooks/useElementSize";
import type { PanelInstance } from "../layout/types";

const baseLayerControls: Array<{ layer: ChartLayerKey; label: string; icon: "candle" | "volume" }> = [
  { layer: "candles", label: "캔들", icon: "candle" },
  { layer: "volume", label: "거래량", icon: "volume" }
];

const movingAverageLayers: Array<{ layer: ChartLayerKey; label: string }> = [
  { layer: "ma5", label: "MA5" },
  { layer: "ma20", label: "MA20" },
  { layer: "ma60", label: "MA60" }
];

type ChartPanelProps = {
  panel: PanelInstance;
  runtime: ChartRuntimeState;
  autoApplyEnabled: boolean;
  backfillEligibleSymbols: readonly SupportedSymbol[];
  onChartAction: (action: ChartRuntimeAction) => void;
  onDevLog?: (entry: ChartDevLogInput) => void;
  onAskAgent: (panelId: string, chartDocumentId: string) => void;
};

type DragAnchor = {
  x: number;
  rightOffset: number;
  visibleCount: number;
};

type DrawingDraft = {
  type: DrawingType;
  first: DrawingAnchor;
};

type DrawingDrag = {
  drawing: DrawingEntity;
  anchor: DrawingAnchor;
  anchorIndex: number | null;
};

type TooltipAttributes = {
  "aria-label": string;
  "data-tooltip": string;
};

const trendLineExtensionOptions: Array<{ value: ChartLineExtension; label: string }> = [
  { value: "segment", label: "구간선" },
  { value: "ray", label: "한쪽 연장선" },
  { value: "line", label: "양방향 연장선" }
];

type FloatingMenuPosition = {
  top: number;
  left: number;
};

type HoverTooltip = FloatingMenuPosition & {
  label: string;
  placement: "bottom" | "right";
};

type ChartRequestIdentity = {
  documentId: string;
  symbol: SupportedSymbol;
  interval: string;
};

type RangeRequestResource = {
  controller: AbortController;
  pollTimer?: number;
};

const liveIdleMessage = "실시간 스트림은 연결됐고 시장 데이터를 기다리는 중입니다.";
const liveRecentlyIdleMessage = "최근 새 실시간 캔들이 없어 저장된 캔들을 사용 중입니다.";
const liveIdleDelayMs = 45_000;
const historicalRangeRequestDebounceMs = 180;

function tooltipAttributes(label: string): TooltipAttributes {
  return {
    "aria-label": label,
    "data-tooltip": label
  };
}

function streamStatusLabel(status: StreamStatus): string {
  switch (status) {
    case "connecting":
      return "연결 중";
    case "idle":
      return "대기";
    case "live":
      return "실시간";
    case "stale":
      return "지연";
    case "error":
      return "오류";
    default:
      return "알 수 없음";
  }
}

function chartToolLabel(toolId: ChartToolMode): string {
  switch (toolId) {
    case "select":
      return "선택";
    case "pan":
      return "이동";
    case "draw-horizontalLine":
      return "수평선";
    case "draw-verticalMarker":
      return "마커";
    case "draw-trendLine":
      return "추세선";
    case "draw-textLabel":
      return "텍스트";
    case "draw-pointMarker":
      return "포인트";
    case "draw-arrow":
      return "화살표";
    case "draw-rangeBox":
      return "범위";
    case "draw-measurement":
      return "측정";
    default:
      return "도구";
  }
}

export function ChartPanel({ panel, runtime, backfillEligibleSymbols, onChartAction, onDevLog, onAskAgent }: ChartPanelProps) {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const sceneRef = useRef<RenderScene | null>(null);
  const renderLogKeyRef = useRef("");
  const dragAnchorRef = useRef<DragAnchor | null>(null);
  const drawingDragRef = useRef<DrawingDrag | null>(null);
  const transientViewportRef = useRef<ChartViewport | null>(null);
  const comparisonRequestsRef = useRef<Set<string>>(new Set());
  const backfillRequestsRef = useRef<Set<string>>(new Set());
  const rangeRequestsRef = useRef<Set<string>>(new Set());
  const rangeRequestResourcesRef = useRef<Map<string, RangeRequestResource>>(new Map());
  const chartRequestIdentityRef = useRef<ChartRequestIdentity | null>(null);
  const { ref: canvasWrapRef, size } = useElementSize<HTMLDivElement>();
  const document = getChartDocumentForPanel(runtime, panel);
  const candles = getCandlesForDocument(runtime, document);
  const dataStatus = getDataStatusForDocument(runtime, document);
  const streamStatus = getStreamStatusForDocument(runtime, document);
  const streamMessage = getStreamMessageForDocument(runtime, document);
  const pendingPreview = runtime.pendingPreviewByDocumentId[document.id];
  const [crosshairPoint, setCrosshairPoint] = useState<{ x: number; y: number } | undefined>();
  const [transientViewport, setTransientViewport] = useState<ChartViewport | null>(null);
  const [transientDrawings, setTransientDrawings] = useState<DrawingEntity[] | null>(null);
  const [drawingDraft, setDrawingDraft] = useState<DrawingDraft | null>(null);
  const [previewPulseKey, setPreviewPulseKey] = useState<string | null>(null);
  const [labelEditorOpen, setLabelEditorOpen] = useState(false);
  const [labelDraft, setLabelDraft] = useState("");
  const [comparisonPickerOpen, setComparisonPickerOpen] = useState(false);
  const [comparisonDraft, setComparisonDraft] = useState("");
  const [comparisonOptions, setComparisonOptions] = useState<SupportedSymbol[]>([]);
  const [maMenuOpen, setMaMenuOpen] = useState(false);
  const [snapshotReloadToken, setSnapshotReloadToken] = useState(0);
  const [rangeReloadToken, setRangeReloadToken] = useState(0);
  const [floatingMenuPosition, setFloatingMenuPosition] = useState<FloatingMenuPosition>({ top: 0, left: 0 });
  const [hoverTooltip, setHoverTooltip] = useState<HoverTooltip | null>(null);
  const selectedDrawing = document.drawings.find((drawing) => drawing.id === document.selectedDrawingId);
  const pendingPreviewKey = pendingPreview ? `${pendingPreview.id}:${pendingPreview.createdAt}` : null;
  const comparisonMatches = comparisonOptions.filter((symbol) => symbol.includes(comparisonDraft.trim().toUpperCase()));
  const comparisonSymbols = Array.from(new Set([
    ...document.comparisons.map((comparison) => comparison.symbol),
    ...(pendingPreview?.comparisons ?? []).map((comparison) => comparison.symbol)
  ]));
  const comparisonSymbolsKey = comparisonSymbols.join("|");
  const comparisonAvailabilityKey = comparisonSymbols.map((symbol) => {
    const key = candleKey(symbol, document.timeframe);
    return `${key}:${runtime.candlesByKey[key]?.length ? "ready" : "missing"}`;
  }).join("|");
  const documentDataKey = candleKey(document.symbol, document.timeframe);
  const backfillEligible = backfillEligibleSymbols.includes(document.symbol);
  const initialBackfillNeeded = shouldRequestBackfill(dataStatus);
  const backfillPreparing = isPreparingCandleData(dataStatus, backfillEligible, backfillRequestsRef.current.has(documentDataKey));
  const chartDataRenderable = isChartDataRenderable(dataStatus);
  const hasActiveMovingAverage = movingAverageLayers.some(({ layer }) => document.layers[layer]);
  chartRequestIdentityRef.current = {
    documentId: document.id,
    symbol: document.symbol,
    interval: document.timeframe
  };
  const sceneDocument = useMemo(
    () => (transientViewport || transientDrawings)
      ? { ...document, viewport: transientViewport ?? document.viewport, drawings: transientDrawings ?? document.drawings }
      : document,
    [document, transientDrawings, transientViewport]
  );
  const target = useMemo(
    () => ({ panelId: panel.id, chartDocumentId: document.id }),
    [document.id, panel.id]
  );
  const logChartDev = useCallback((entry: ChartDevLogInput) => {
    // DEV-ONLY: 차트/backfill 개발 완료 후 이 진단 로그 호출부는 제거한다.
    onDevLog?.({
      symbol: document.symbol,
      interval: document.timeframe,
      chartDocumentId: document.id,
      panelId: panel.id,
      ...entry
    });
  }, [document.id, document.symbol, document.timeframe, onDevLog, panel.id]);

  useEffect(() => {
    return () => {
      for (const [requestKey, resource] of rangeRequestResourcesRef.current.entries()) {
        if (resource.pollTimer) {
          window.clearTimeout(resource.pollTimer);
        }
        resource.controller.abort();
        rangeRequestsRef.current.delete(requestKey);
      }
      rangeRequestResourcesRef.current.clear();
    };
  }, []);

  useEffect(() => {
    if (!pendingPreviewKey) {
      setPreviewPulseKey(null);
      return undefined;
    }
    setPreviewPulseKey(pendingPreviewKey);
    const timer = window.setTimeout(() => {
      setPreviewPulseKey((current) => (current === pendingPreviewKey ? null : current));
    }, 1400);
    return () => window.clearTimeout(timer);
  }, [pendingPreviewKey]);

  useEffect(() => {
    setLabelDraft(selectedDrawing?.label ?? defaultDrawingLabel(selectedDrawing?.type) ?? "");
    if (!selectedDrawing) {
      setLabelEditorOpen(false);
    }
  }, [selectedDrawing?.id, selectedDrawing?.label, selectedDrawing?.type]);

  useEffect(() => {
    if (!hoverTooltip) {
      return undefined;
    }
    const clearTooltipOutsidePanel = (event: PointerEvent) => {
      const targetNode = event.target instanceof Node ? event.target : null;
      if (panelRef.current && targetNode && !panelRef.current.contains(targetNode)) {
        setHoverTooltip(null);
      }
    };
    window.addEventListener("pointermove", clearTooltipOutsidePanel, true);
    return () => window.removeEventListener("pointermove", clearTooltipOutsidePanel, true);
  }, [hoverTooltip]);

  useEffect(() => {
    if (!comparisonPickerOpen) {
      setComparisonOptions([]);
      return undefined;
    }

    let cancelled = false;
    const controller = new AbortController();
    const params = new URLSearchParams({
      q: comparisonDraft.trim(),
      limit: "20"
    });

    fetch(`/api/market/symbols/search?${params.toString()}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`종목 검색 API 응답 오류 ${response.status}`);
        }
        return response.json() as Promise<unknown>;
      })
      .then((payload) => {
        if (cancelled) {
          return;
        }
        setComparisonOptions(
          normalizeWatchlistPayload(payload)
            .map((item) => item.symbol)
            .filter((symbol) => symbol !== document.symbol)
        );
      })
      .catch((error: unknown) => {
        if (!cancelled && !isAbortError(error)) {
          setComparisonOptions([]);
        }
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [comparisonDraft, comparisonPickerOpen, document.symbol]);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    const params = new URLSearchParams({
      symbol: document.symbol,
      interval: document.timeframe,
      ma: "5,20,60",
      limit: String(defaultVisibleBarsForInterval(document.timeframe))
    });

    logChartDev({
      level: "info",
      category: "chart-data",
      message: "Requesting candle snapshot",
      details: {
        limit: defaultVisibleBarsForInterval(document.timeframe),
        movingAverages: "5,20,60"
      }
    });
    onChartAction({ kind: "chart.stream.status", symbol: document.symbol, interval: document.timeframe, status: "connecting" });

    fetch(`/api/charts/candles?${params.toString()}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`캔들 API 응답 오류 ${response.status}`);
        }
        return response.json() as Promise<unknown>;
      })
      .then((payload) => {
        if (cancelled) {
          return;
        }
        const snapshot = normalizeCandleSnapshot(payload);
        logChartDev({
          level: snapshot.candles.length ? "info" : "warn",
          category: "chart-data",
          message: "Candle snapshot response received",
          symbol: snapshot.symbol,
          interval: snapshot.interval,
          details: {
            candleCount: snapshot.candles.length,
            returnedCount: snapshot.returnedCount,
            storedCandleCount: snapshot.storedCandleCount,
            dataStatus: snapshot.dataStatus,
            backfillStatus: snapshot.backfillStatus,
            repairStatus: snapshot.repairStatus,
            sourceInterval: snapshot.sourceInterval,
            requestedRange: snapshot.requestedRange,
            oldestTimestamp: snapshot.oldestTimestamp,
            newestTimestamp: snapshot.newestTimestamp,
            availableFrom: snapshot.availableFrom,
            availableTo: snapshot.availableTo,
            noDataBefore: snapshot.noDataBefore,
            hasMoreBefore: snapshot.hasMoreBefore,
            coverageSummary: summarizeChartCoverage(snapshot.coverage)
          }
        });
        onChartAction({ kind: "chart.snapshot.loaded", snapshot });
      })
      .catch((error: unknown) => {
        if (cancelled || isAbortError(error)) {
          return;
        }
        logChartDev({
          level: "error",
          category: "chart-data",
          message: "Candle snapshot request failed",
          details: {
            error: error instanceof Error ? error.message : String(error)
          }
        });
        onChartAction({
          kind: "chart.snapshot.failed",
          symbol: document.symbol,
          interval: document.timeframe,
          message: "시장 데이터를 불러올 수 없습니다."
        });
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [document.symbol, document.timeframe, logChartDev, onChartAction, snapshotReloadToken]);

  useEffect(() => {
    const key = candleKey(document.symbol, document.timeframe);
    const minimumSourceBars = minimumBackfillSourceBarsForInterval(document.timeframe);
    const bufferMultiplier = rangeBackfillBufferMultiplierForInterval(document.timeframe);
    const windowRange = rangeBackfillWindow(
      document.timeframe,
      new Date().toISOString(),
      defaultVisibleBarsForInterval(document.timeframe),
      { bufferMultiplier, minimumSourceBars }
    );
    if (
      !backfillEligible ||
      !initialBackfillNeeded ||
      !windowRange ||
      backfillRequestsRef.current.has(key)
    ) {
      return undefined;
    }

    backfillRequestsRef.current.add(key);
    let cancelled = false;
    let pollTimer: number | undefined;
    const controller = new AbortController();

    logChartDev({
      level: "info",
      category: "backfill",
      message: "Initial backfill requested",
      details: {
        key,
        requestedInterval: document.timeframe,
        sourceInterval: dataStatus.sourceInterval ?? document.timeframe,
        start: windowRange.start,
        end: windowRange.end,
        dataState: dataStatus.state,
        requestedBars: defaultVisibleBarsForInterval(document.timeframe),
        bufferMultiplier,
        minimumSourceBars
      }
    });

    const applyBackfillStatus = (payload: unknown) => {
      const status = normalizeBackfillStatusPayload(payload);
      if (cancelled) {
        return;
      }

      logChartDev({
        level: backfillDevLogLevel(status.status),
        category: "backfill",
        message: `Initial backfill status: ${status.status}`,
        details: {
          requestId: status.requestId,
          requestedInterval: status.interval ?? document.timeframe,
          error: status.error,
          sourceInterval: status.sourceInterval,
          resultSummary: summarizeBackfillResult(status.result),
          result: status.result
        }
      });

      if (status.status === "succeeded") {
        backfillRequestsRef.current.delete(key);
        setSnapshotReloadToken((current) => current + 1);
        return;
      }

      if (isActiveBackfillStatus(status.status)) {
        pollTimer = window.setTimeout(() => {
          pollBackfillStatus(status.requestId);
        }, 1200);
        return;
      }

      backfillRequestsRef.current.delete(key);
      onChartAction({
        kind: "chart.data.status",
        symbol: document.symbol,
        interval: document.timeframe,
        status: {
          state: status.status === "failed" || status.status === "unavailable" ? "error" : "empty",
          message: backfillStatusMessage(status.status, status.error),
          source: dataStatus.source,
          feed: dataStatus.feed,
          backfillStatus: status.status,
          canBackfill: !isActiveBackfillStatus(status.status) && status.status !== "unavailable",
          sourceInterval: status.sourceInterval
        }
      });
    };

    const pollBackfillStatus = (requestId?: string) => {
      const params = new URLSearchParams({
        symbol: document.symbol,
        interval: document.timeframe
      });
      if (requestId) {
        params.set("requestId", requestId);
      }

      fetch(`/api/charts/backfill/status?${params.toString()}`, { signal: controller.signal })
        .then((response) => {
          if (!response.ok) {
            throw new Error(`백필 상태 API 응답 오류 ${response.status}`);
          }
          return response.json() as Promise<unknown>;
        })
        .then(applyBackfillStatus)
        .catch((error: unknown) => {
          if (cancelled || isAbortError(error)) {
            return;
          }
          backfillRequestsRef.current.delete(key);
          logChartDev({
            level: "error",
            category: "backfill",
            message: "Initial backfill status check failed",
            details: {
              requestedInterval: document.timeframe,
              sourceInterval: dataStatus.sourceInterval ?? document.timeframe,
              error: error instanceof Error ? error.message : String(error)
            }
          });
          onChartAction({
            kind: "chart.data.status",
            symbol: document.symbol,
            interval: document.timeframe,
            status: {
              state: "error",
              message: error instanceof Error ? error.message : "Backfill status check failed.",
              source: dataStatus.source,
              feed: dataStatus.feed,
              backfillStatus: "failed",
              canBackfill: true,
              sourceInterval: dataStatus.sourceInterval ?? document.timeframe
            }
          });
        });
    };

    fetch("/api/charts/backfill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: JSON.stringify({
        symbol: document.symbol,
        interval: document.timeframe,
        start: windowRange.start,
        end: windowRange.end
      })
    })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`백필 API 응답 오류 ${response.status}`);
        }
        return response.json() as Promise<unknown>;
      })
      .then(applyBackfillStatus)
      .catch((error: unknown) => {
        if (cancelled || isAbortError(error)) {
          return;
        }
        backfillRequestsRef.current.delete(key);
        logChartDev({
          level: "error",
          category: "backfill",
          message: "Initial backfill request failed",
          details: {
            requestedInterval: document.timeframe,
            sourceInterval: dataStatus.sourceInterval ?? document.timeframe,
            start: windowRange.start,
            end: windowRange.end,
            error: error instanceof Error ? error.message : String(error)
          }
        });
        onChartAction({
          kind: "chart.data.status",
          symbol: document.symbol,
          interval: document.timeframe,
          status: {
            state: "error",
            message: error instanceof Error ? error.message : "Backfill request failed.",
            source: dataStatus.source,
            feed: dataStatus.feed,
            backfillStatus: "failed",
            canBackfill: true,
            sourceInterval: dataStatus.sourceInterval ?? document.timeframe
          }
        });
      });

    return () => {
      cancelled = true;
      controller.abort();
      if (pollTimer) {
        window.clearTimeout(pollTimer);
      }
    };
  }, [
    backfillEligible,
    dataStatus.backfillStatus,
    dataStatus.canBackfill,
    dataStatus.feed,
    dataStatus.repairStatus,
    dataStatus.source,
    dataStatus.sourceInterval,
    dataStatus.state,
    document.symbol,
    document.timeframe,
    initialBackfillNeeded,
    logChartDev,
    onChartAction
  ]);

  useEffect(() => {
    if (typeof window === "undefined" || !("WebSocket" in window)) {
      logChartDev({
        level: "warn",
        category: "stream",
        message: "WebSocket API unavailable"
      });
      return;
    }

    const params = new URLSearchParams({
      symbol: document.symbol,
      interval: document.timeframe
    });
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let idleTimer: number | undefined;
    let closedByEffect = false;
    let reconnectAttempt = 0;
    let sawLiveCandle = false;

    const clearIdleTimer = () => {
      if (idleTimer) {
        window.clearTimeout(idleTimer);
        idleTimer = undefined;
      }
    };

    const markIdle = (message = liveIdleMessage) => {
      onChartAction({
        kind: "chart.stream.status",
        symbol: document.symbol,
        interval: document.timeframe,
        status: "idle",
        message
      });
    };

    const scheduleIdle = (message = liveRecentlyIdleMessage) => {
      clearIdleTimer();
      idleTimer = window.setTimeout(() => {
        markIdle(message);
      }, liveIdleDelayMs);
    };

    const connect = () => {
      logChartDev({
        level: "debug",
        category: "stream",
        message: "Opening chart websocket",
        details: {
          reconnectAttempt
        }
      });
      onChartAction({ kind: "chart.stream.status", symbol: document.symbol, interval: document.timeframe, status: "connecting" });
      socket = new WebSocket(resolveChartSocketUrl(params));

      socket.onopen = () => {
        reconnectAttempt = 0;
        sawLiveCandle = false;
        logChartDev({
          level: "info",
          category: "stream",
          message: "Chart websocket opened"
        });
        markIdle();
        scheduleIdle();
      };

      socket.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          if (isRealtimeControlPayload(payload)) {
            if (payload.type === "HEARTBEAT" || payload.type === "MARKET_STATUS_UPDATE") {
              if (!sawLiveCandle) {
                markIdle();
              }
              scheduleIdle();
              return;
            }
            if (payload.type === "ERROR") {
              logChartDev({
                level: payload.retryable === true ? "warn" : "error",
                category: "stream",
                message: "Chart websocket control error",
                details: {
                  retryable: payload.retryable,
                  detail: payload.detail
                }
              });
              onChartAction({
                kind: "chart.stream.status",
                symbol: document.symbol,
                interval: document.timeframe,
                status: payload.retryable === true ? "stale" : "error",
                message: typeof payload.detail === "string" ? payload.detail : "Live candle stream error."
              });
              return;
            }
            return;
          }
          sawLiveCandle = true;
          scheduleIdle();
          onChartAction({ kind: "chart.live", event: normalizeCandleEvent(payload) });
        } catch (error) {
          clearIdleTimer();
          logChartDev({
            level: "error",
            category: "stream",
            message: "Invalid live candle event",
            details: {
              error: error instanceof Error ? error.message : String(error)
            }
          });
          onChartAction({
            kind: "chart.stream.status",
            symbol: document.symbol,
            interval: document.timeframe,
            status: "error",
            message: error instanceof Error ? error.message : "Invalid live candle event"
          });
        }
      };

      socket.onerror = () => {
        clearIdleTimer();
        logChartDev({
          level: "warn",
          category: "stream",
          message: "Chart websocket transport error; reconnecting"
        });
        onChartAction({
          kind: "chart.stream.status",
          symbol: document.symbol,
          interval: document.timeframe,
          status: "stale",
          message: "Live candle stream reconnecting..."
        });
      };

      socket.onclose = () => {
        clearIdleTimer();
        if (closedByEffect) {
          return;
        }
        onChartAction({ kind: "chart.stream.status", symbol: document.symbol, interval: document.timeframe, status: "stale" });
        const delay = Math.min(3000, 600 + reconnectAttempt * 400);
        logChartDev({
          level: "warn",
          category: "stream",
          message: "Chart websocket closed; reconnect scheduled",
          details: {
            reconnectAttempt,
            delayMs: delay
          }
        });
        reconnectAttempt += 1;
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      closedByEffect = true;
      if (reconnectTimer) {
        window.clearTimeout(reconnectTimer);
      }
      clearIdleTimer();
      socket?.close();
    };
  }, [document.symbol, document.timeframe, logChartDev, onChartAction]);

  const scene = useMemo(() => {
    const nextScene = buildRenderScene({
      state: backfillPreparing ? "loading" : chartDataRenderable ? "ready" : dataStatus.state,
      message: backfillPreparing ? "Preparing candle data..." : dataStatus.message,
      document: sceneDocument,
      candles,
      width: size.width,
      height: size.height,
      crosshair: crosshairPoint,
      streamStatus,
      comparisonCandlesBySymbol: Object.fromEntries(
        comparisonSymbols.map((symbol) => [
          symbol,
          runtime.candlesByKey[candleKey(symbol, document.timeframe)] ?? []
        ])
      ),
      pendingPreview
    });
    sceneRef.current = nextScene;
    return nextScene;
  }, [backfillPreparing, candles, chartDataRenderable, comparisonSymbols, crosshairPoint, dataStatus.message, dataStatus.state, document.timeframe, pendingPreview, runtime.candlesByKey, sceneDocument, size.height, size.width, streamStatus]);

  useEffect(() => {
    const controllers: AbortController[] = [];
    const comparisonSymbols = comparisonSymbolsKey
      ? comparisonSymbolsKey.split("|").filter(Boolean) as SupportedSymbol[]
      : [];
    const readyComparisonKeys = new Set(
      comparisonAvailabilityKey
        .split("|")
        .filter((entry) => entry.endsWith(":ready"))
        .map((entry) => entry.slice(0, entry.lastIndexOf(":")))
    );

    comparisonSymbols.forEach((symbol) => {
      const key = candleKey(symbol, document.timeframe);
      if (readyComparisonKeys.has(key)) {
        return;
      }
      if (comparisonRequestsRef.current.has(key)) {
        return;
      }
      comparisonRequestsRef.current.add(key);
      const controller = new AbortController();
      controllers.push(controller);
      const params = new URLSearchParams({
        symbol,
        interval: document.timeframe,
        ma: "5,20,60",
        limit: String(defaultVisibleBarsForInterval(document.timeframe))
      });
      fetch(`/api/charts/candles?${params.toString()}`, { signal: controller.signal })
        .then((response) => {
          if (!response.ok) {
            throw new Error(`비교 캔들 API 응답 오류 ${response.status}`);
          }
          return response.json() as Promise<unknown>;
        })
        .then((payload) => onChartAction({ kind: "chart.snapshot.loaded", snapshot: normalizeCandleSnapshot(payload) }))
        .catch((error: unknown) => {
          if (isAbortError(error)) {
            return;
          }
          onChartAction({
            kind: "chart.error",
            chartDocumentId: document.id,
            message: error instanceof Error ? error.message : "Comparison data unavailable."
          });
        })
        .finally(() => {
          comparisonRequestsRef.current.delete(key);
        });
    });

    return () => {
      controllers.forEach((controller) => controller.abort());
    };
  }, [comparisonAvailabilityKey, comparisonSymbolsKey, document.id, document.timeframe, onChartAction]);

  useEffect(() => {
    if (!candles.length || !dataStatus.hasMoreBefore) {
      return undefined;
    }

    const plotWidth = Math.max(1, scene.plot.right - scene.plot.left);
    const rangeViewport = normalizeViewport(document.viewport, candles.length, plotWidth);
    const rangePlan = planHistoricalRangeLoad({
      symbol: document.symbol,
      interval: document.timeframe,
      candleCount: candles.length,
      oldestTimestamp: candles[0]?.timestamp,
      rightOffset: rangeViewport.rightOffset,
      visibleCount: rangeViewport.visibleCount,
      hasMoreBefore: dataStatus.hasMoreBefore,
      noDataBefore: dataStatus.noDataBefore
    });
    if (!rangePlan) {
      return undefined;
    }

    const {
      requestKey,
      before,
      pageLimit,
      plannedBackfillRange,
      defaultVisibleCount,
      minimumSourceBars,
      targetVisibleCount,
      visibleEnd,
      visibleStart,
      bufferedVisibleCount,
      bufferMultiplier,
      loadedOldestLookaheadCount,
      candleCount,
      userZoomedPastDefault,
      userPannedIntoHistory,
      isLookingPastLoadedRange,
      isNearLoadedOldest
    } = rangePlan;
    const historicalReadRequest = historicalRangeReadRequest(rangePlan);
    if (rangeRequestsRef.current.has(requestKey)) {
      return undefined;
    }

    rangeRequestsRef.current.add(requestKey);
    const requestIdentity: ChartRequestIdentity = {
      documentId: document.id,
      symbol: document.symbol,
      interval: document.timeframe
    };
    logChartDev({
      level: "info",
      category: "backfill",
      message: "Historical range requested",
      details: {
        requestKey,
        requestedInterval: document.timeframe,
        sourceInterval: dataStatus.sourceInterval ?? document.timeframe,
        oldest: before,
        visibleStart,
        visibleEnd,
        targetVisibleCount,
        bufferedVisibleCount,
        bufferMultiplier,
        loadedOldestLookaheadCount,
        defaultVisibleCount,
        minimumSourceBars,
        pageLimit,
        plannedBackfillRange,
        historicalReadRequest,
        rangeViewport,
        requestedViewport: document.viewport,
        candleCount,
        userZoomedPastDefault,
        userPannedIntoHistory,
        isLookingPastLoadedRange,
        isNearLoadedOldest
      }
    });
    const params = new URLSearchParams({
      symbol: document.symbol,
      interval: document.timeframe,
      ma: "5,20,60",
      limit: String(historicalReadRequest.limit)
    });
    if (historicalReadRequest.from && historicalReadRequest.to) {
      params.set("from", historicalReadRequest.from);
      params.set("to", historicalReadRequest.to);
    } else if (historicalReadRequest.before) {
      params.set("before", historicalReadRequest.before);
    }
    const controller = new AbortController();
    const resource: RangeRequestResource = { controller };
    rangeRequestResourcesRef.current.set(requestKey, resource);
    let debounceTimer: number | undefined;
    let rangeBackfillStarted = false;
    let cancelled = false;

    const isCurrentRangeRequest = () => {
      const current = chartRequestIdentityRef.current;
      return !cancelled &&
        current?.documentId === requestIdentity.documentId &&
        current.symbol === requestIdentity.symbol &&
        current.interval === requestIdentity.interval &&
        rangeRequestResourcesRef.current.get(requestKey) === resource;
    };

    const releaseRangeRequest = (abort = false) => {
      const currentResource = rangeRequestResourcesRef.current.get(requestKey);
      if (currentResource !== resource) {
        return;
      }
      if (resource.pollTimer) {
        window.clearTimeout(resource.pollTimer);
        resource.pollTimer = undefined;
      }
      if (abort) {
        resource.controller.abort();
      }
      rangeRequestResourcesRef.current.delete(requestKey);
      rangeRequestsRef.current.delete(requestKey);
    };

    const scheduleRangeStatusPoll = (requestId?: string) => {
      if (resource.pollTimer) {
        window.clearTimeout(resource.pollTimer);
      }
      resource.pollTimer = window.setTimeout(() => {
        resource.pollTimer = undefined;
        pollRangeBackfillStatus(requestId);
      }, 1200);
    };

    const applyRangeBackfillStatus = (payload: unknown) => {
      const status = normalizeBackfillStatusPayload(payload);
      if (!isCurrentRangeRequest()) {
        return;
      }
      logChartDev({
        level: backfillDevLogLevel(status.status),
        category: "backfill",
        message: `Range backfill status: ${status.status}`,
        details: {
          requestId: status.requestId,
          requestedInterval: status.interval ?? document.timeframe,
          error: status.error,
          sourceInterval: status.sourceInterval,
          resultSummary: summarizeBackfillResult(status.result),
          result: status.result,
          requestKey
        }
      });
      if (status.status === "succeeded") {
        releaseRangeRequest();
        setRangeReloadToken((current) => current + 1);
        return;
      }

      if (isActiveBackfillStatus(status.status)) {
        scheduleRangeStatusPoll(status.requestId);
        onChartAction({
          kind: "chart.data.status",
          symbol: document.symbol,
          interval: document.timeframe,
          status: {
            state: dataStatus.state,
            message: "Loading earlier candles...",
            source: dataStatus.source,
            feed: dataStatus.feed,
            backfillStatus: status.status,
            canBackfill: true,
            sourceInterval: status.sourceInterval ?? dataStatus.sourceInterval ?? document.timeframe,
            coverage: dataStatus.coverage,
            hasMoreBefore: dataStatus.hasMoreBefore
          }
        });
        return;
      }

      releaseRangeRequest();
      onChartAction({
        kind: "chart.error",
        chartDocumentId: document.id,
        message: backfillStatusMessage(status.status, status.error)
      });
    };

    const pollRangeBackfillStatus = (requestId?: string) => {
      if (!isCurrentRangeRequest()) {
        return;
      }
      const statusParams = new URLSearchParams({
        symbol: document.symbol,
        interval: document.timeframe
      });
      if (requestId) {
        statusParams.set("requestId", requestId);
      }

      fetch(`/api/charts/backfill/status?${statusParams.toString()}`, { signal: controller.signal })
        .then((response) => {
          if (!response.ok) {
            throw new Error(`Range backfill status API returned ${response.status}`);
          }
          return response.json() as Promise<unknown>;
        })
        .then(applyRangeBackfillStatus)
        .catch((error: unknown) => {
          if (isAbortError(error) || !isCurrentRangeRequest()) {
            return;
          }
          releaseRangeRequest();
          logChartDev({
            level: "error",
            category: "backfill",
            message: "Range backfill status check failed",
            details: {
              requestKey,
              requestedInterval: document.timeframe,
              sourceInterval: dataStatus.sourceInterval ?? document.timeframe,
              error: error instanceof Error ? error.message : String(error)
            }
          });
          onChartAction({
            kind: "chart.error",
            chartDocumentId: document.id,
            message: error instanceof Error ? error.message : "Range backfill status check failed."
          });
        });
    };

    const requestRangeBackfill = () => {
      if (!isCurrentRangeRequest()) {
        return;
      }
      if (!backfillEligible) {
        logChartDev({
          level: "warn",
          category: "backfill",
          message: "Range backfill skipped: symbol is not eligible",
          details: {
            requestKey,
            requestedInterval: document.timeframe,
            sourceInterval: dataStatus.sourceInterval ?? document.timeframe,
            plannedBackfillRange
          }
        });
        return;
      }
      const windowRange = plannedBackfillRange;
      if (!windowRange) {
        logChartDev({
          level: "warn",
          category: "backfill",
          message: "Range backfill skipped: no request window",
          details: {
            requestKey,
            requestedInterval: document.timeframe,
            sourceInterval: dataStatus.sourceInterval ?? document.timeframe,
            oldest: before,
            targetVisibleCount,
            minimumSourceBars,
            pageLimit
          }
        });
        return;
      }

      rangeBackfillStarted = true;
      logChartDev({
        level: "info",
        category: "backfill",
        message: "Range backfill requested",
        details: {
          requestKey,
          requestedInterval: document.timeframe,
          sourceInterval: dataStatus.sourceInterval ?? document.timeframe,
          start: windowRange.start,
          end: windowRange.end,
          minimumSourceBars
        }
      });
      fetch("/api/charts/backfill", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          symbol: document.symbol,
          interval: document.timeframe,
          start: windowRange.start,
          end: windowRange.end
        })
      })
        .then((response) => {
          if (!response.ok) {
            throw new Error(`Range backfill API returned ${response.status}`);
          }
          return response.json() as Promise<unknown>;
        })
        .then(applyRangeBackfillStatus)
        .catch((error: unknown) => {
          if (isAbortError(error) || !isCurrentRangeRequest()) {
            return;
          }
          releaseRangeRequest();
          logChartDev({
            level: "error",
            category: "backfill",
            message: "Range backfill request failed",
            details: {
              requestKey,
              requestedInterval: document.timeframe,
              sourceInterval: dataStatus.sourceInterval ?? document.timeframe,
              error: error instanceof Error ? error.message : String(error)
            }
          });
          onChartAction({
            kind: "chart.error",
            chartDocumentId: document.id,
            message: error instanceof Error ? error.message : "Range backfill request failed."
          });
        });
    };

    debounceTimer = window.setTimeout(() => {
      fetch(`/api/charts/candles?${params.toString()}`, { signal: controller.signal })
        .then((response) => {
          if (!response.ok) {
            throw new Error(`과거 구간 API 응답 오류 ${response.status}`);
          }
          return response.json() as Promise<unknown>;
        })
        .then((payload) => {
          if (!isCurrentRangeRequest()) {
            return;
          }
          const snapshot = normalizeCandleSnapshot(payload);
          const needsBackfill = shouldRequestRangeBackfill(snapshot);
          logChartDev({
            level: snapshot.candles.length ? "info" : "warn",
            category: "chart-data",
            message: "Historical range response received",
            symbol: snapshot.symbol,
            interval: snapshot.interval,
            details: {
              requestKey,
              sourceInterval: snapshot.sourceInterval,
              before,
              pageLimit,
              plannedBackfillRange,
              historicalReadRequest,
              candleCount: snapshot.candles.length,
              returnedCount: snapshot.returnedCount,
              storedCandleCount: snapshot.storedCandleCount,
              availableFrom: snapshot.availableFrom,
              availableTo: snapshot.availableTo,
              noDataBefore: snapshot.noDataBefore,
              snapshotSource: snapshot.source,
              dataStatus: snapshot.dataStatus,
              backfillStatus: snapshot.backfillStatus,
              repairStatus: snapshot.repairStatus,
              requestedRange: snapshot.requestedRange,
              hasMoreBefore: snapshot.hasMoreBefore,
              coverageSummary: summarizeChartCoverage(snapshot.coverage),
              needsBackfill
            }
          });
          onChartAction({ kind: "chart.snapshot.loaded", snapshot });
          if (needsBackfill) {
            requestRangeBackfill();
          }
        })
        .catch((error: unknown) => {
          if (isAbortError(error) || !isCurrentRangeRequest()) {
            return;
          }
          logChartDev({
            level: "error",
            category: "chart-data",
            message: "Historical range request failed",
            details: {
              requestKey,
              requestedInterval: document.timeframe,
              sourceInterval: dataStatus.sourceInterval ?? document.timeframe,
              before,
              pageLimit,
              historicalReadRequest,
              error: error instanceof Error ? error.message : String(error)
            }
          });
          onChartAction({
            kind: "chart.error",
            chartDocumentId: document.id,
            message: error instanceof Error ? error.message : "Historical range data unavailable."
          });
        })
        .finally(() => {
          if (rangeBackfillStarted) {
            return;
          }
          releaseRangeRequest();
        });
    }, historicalRangeRequestDebounceMs);

    return () => {
      if (debounceTimer) {
        window.clearTimeout(debounceTimer);
      }
      const current = chartRequestIdentityRef.current;
      const sameChartIdentity = current?.documentId === requestIdentity.documentId &&
        current.symbol === requestIdentity.symbol &&
        current.interval === requestIdentity.interval;
      if (rangeBackfillStarted && sameChartIdentity) {
        return;
      }
      cancelled = true;
      releaseRangeRequest(true);
    };
  }, [
    candles,
    backfillEligible,
    dataStatus.hasMoreBefore,
    dataStatus.coverage,
    dataStatus.feed,
    dataStatus.source,
    dataStatus.sourceInterval,
    dataStatus.state,
    document.id,
    document.symbol,
    document.timeframe,
    document.viewport.rightOffset,
    document.viewport.visibleCount,
    logChartDev,
    onChartAction,
    rangeReloadToken,
    scene.plot.left,
    scene.plot.right
  ]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || size.width <= 0 || size.height <= 0) {
      const renderKey = `skip:${Boolean(canvas)}:${size.width}:${size.height}`;
      if (renderLogKeyRef.current !== renderKey) {
        renderLogKeyRef.current = renderKey;
        logChartDev({
          level: "warn",
          category: "render",
          message: "Chart render skipped",
          details: {
            hasCanvas: Boolean(canvas),
            width: size.width,
            height: size.height
          }
        });
      }
      return;
    }

    if (scene.state !== "ready" || scene.candles.length === 0) {
      const renderKey = `state:${scene.state}:${scene.candles.length}:${size.width}:${size.height}:${scene.message ?? ""}`;
      if (renderLogKeyRef.current !== renderKey) {
        renderLogKeyRef.current = renderKey;
        logChartDev({
          level: scene.state === "error" ? "error" : "warn",
          category: "render",
          message: "Chart render is not ready",
          details: {
            state: scene.state,
            message: scene.message,
            candleCount: scene.candles.length,
            width: size.width,
            height: size.height
          }
        });
      }
    }

    try {
      drawChartScene(canvas, scene);
      if (scene.state === "ready" && scene.candles.length > 0) {
        const renderKey = `ready:${document.symbol}:${document.timeframe}:${scene.candles.length}:${size.width}:${size.height}`;
        if (renderLogKeyRef.current !== renderKey) {
          renderLogKeyRef.current = renderKey;
          logChartDev({
            level: "debug",
            category: "render",
            message: "Chart scene rendered",
            details: {
              candleCount: scene.candles.length,
              width: size.width,
              height: size.height
            }
          });
        }
      }
    } catch (error) {
      const renderKey = `error:${document.symbol}:${document.timeframe}:${size.width}:${size.height}`;
      renderLogKeyRef.current = renderKey;
      logChartDev({
        level: "error",
        category: "render",
        message: "Chart render failed",
        details: {
          error: error instanceof Error ? error.message : String(error),
          state: scene.state,
          candleCount: scene.candles.length,
          width: size.width,
          height: size.height
        }
      });
      onChartAction({
        kind: "chart.error",
        chartDocumentId: document.id,
        message: error instanceof Error ? error.message : "Chart render failed."
      });
    }
  }, [document.id, document.symbol, document.timeframe, logChartDev, onChartAction, scene, size.height, size.width]);

  const placeFloatingMenu = (element: HTMLElement, width = 190) => {
    const rect = element.getBoundingClientRect();
    const left = Math.min(Math.max(8, rect.left), Math.max(8, window.innerWidth - width - 8));
    setFloatingMenuPosition({ top: rect.bottom + 6, left });
  };

  const updateHoverTooltip = (event: ReactPointerEvent<HTMLElement>) => {
    const tooltipTarget = readTooltipTarget(event.target, event.currentTarget);
    if (!tooltipTarget) {
      setHoverTooltip(null);
      return;
    }
    const label = tooltipTarget.dataset.tooltip;
    if (!label) {
      setHoverTooltip(null);
      return;
    }
    const rect = tooltipTarget.getBoundingClientRect();
    const placement = tooltipTarget.closest(".chart-drawing-rail") ? "right" : "bottom";
    const nextTooltip: HoverTooltip = placement === "right"
      ? { label, placement, top: rect.top + rect.height / 2, left: rect.right + 7 }
      : { label, placement, top: rect.bottom + 6, left: rect.left + rect.width / 2 };
    setHoverTooltip((current) => (
      current &&
      current.label === nextTooltip.label &&
      current.placement === nextTooltip.placement &&
      Math.abs(current.top - nextTooltip.top) < 0.5 &&
      Math.abs(current.left - nextTooltip.left) < 0.5
        ? current
        : nextTooltip
    ));
  };

  const runCommand = (type: Parameters<typeof makeChartCommand>[0], payload: Record<string, unknown> = {}) => {
    onChartAction({ kind: "chart.command", command: makeChartCommand(type, "user", target, payload, undefined, "chartPanel") });
  };

  const setDocumentToolMode = (
    mode: ChartToolMode,
    trendLineExtension = document.interactionState.trendLineExtension,
    options: { preserveDraft?: boolean } = {}
  ) => {
    if (!options.preserveDraft) {
      setDrawingDraft(null);
      setTransientDrawings(null);
    }
    onChartAction({
      kind: "chart.command",
      command: makeChartCommand(
        "chart.drawing.clearSelection",
        "system",
        target,
        { mode, trendLineExtension },
        undefined,
        "chartPanel"
      )
    });
  };

  const saveSelectedDrawingLabel = () => {
    if (!selectedDrawing) {
      return;
    }
    runCommand("chart.drawing.update", {
      drawingId: selectedDrawing.id,
      drawingPatch: {
        label: labelDraft.trim()
      }
    });
    setLabelEditorOpen(false);
  };

  const updateSelectedDrawingStyle = () => {
    if (!selectedDrawing) {
      return;
    }
    const nextColor = selectedDrawing.style.color === "#dc2626" ? defaultDrawingStyle(selectedDrawing.type).color : "#dc2626";
    runCommand("chart.drawing.update", {
      drawingId: selectedDrawing.id,
      drawingPatch: {
        style: { ...selectedDrawing.style, color: nextColor, textColor: nextColor }
      }
    });
  };

  const removeSelectedDrawing = () => {
    if (!selectedDrawing) {
      return;
    }
    runCommand("chart.drawing.remove", { drawingId: selectedDrawing.id });
  };

  const clearAllDrawings = () => {
    const commands = document.drawings.map((drawing) =>
      makeChartCommand(
        "chart.drawing.remove",
        "user",
        target,
        { drawingId: drawing.id },
        undefined,
        "chartPanel"
      )
    );
    onChartAction({ kind: "chart.command.group", commands, label: "Clear all chart drawings" });
  };

  const addComparison = (symbol: SupportedSymbol) => {
    if (symbol === document.symbol || document.comparisons.some((comparison) => comparison.symbol === symbol)) {
      return;
    }
    runCommand("chart.comparison.add", {
      comparison: {
        id: `comparison-${symbol.toLowerCase()}-${document.id}`,
        symbol,
        label: symbol,
        scaleMode: "percent",
        base: { mode: "visibleRangeStart" },
        style: { color: comparisonColor(symbol), lineWidth: 1.5 }
      }
    });
    setComparisonPickerOpen(false);
    setComparisonDraft("");
  };

  const removeComparison = (comparisonId: string) => {
    runCommand("chart.comparison.remove", { comparisonId });
    setComparisonPickerOpen(false);
    setComparisonDraft("");
  };

  const toggleComparison = (symbol: SupportedSymbol) => {
    const comparison = document.comparisons.find((item) => item.symbol === symbol);
    if (comparison) {
      removeComparison(comparison.id);
      return;
    }
    addComparison(symbol);
  };

  const submitComparisonDraft = () => {
    const symbol = normalizeSupportedSymbol(comparisonDraft);
    if (symbol) {
      toggleComparison(symbol);
    }
  };

  const getPlotWidth = () => {
    const currentScene = sceneRef.current;
    return currentScene ? currentScene.plot.right - currentScene.plot.left : undefined;
  };

  const normalizePanelViewport = (viewport: ChartViewport) => (
    normalizeViewport(viewport, candles.length, getPlotWidth())
  );

  const setViewport = (visibleCount: number, rightOffset = document.viewport.rightOffset) => {
    const nextViewport = normalizePanelViewport({ visibleCount, rightOffset });
    runCommand("chart.viewport.set", nextViewport);
  };

  const applyViewport = (nextViewport: ChartViewport) => {
    if (
      nextViewport.visibleCount === document.viewport.visibleCount &&
      nextViewport.rightOffset === document.viewport.rightOffset
    ) {
      return;
    }
    runCommand("chart.viewport.set", nextViewport);
  };

  const zoomBy = (delta: number) => {
    applyViewport(zoomViewport(document.viewport, delta, candles.length, getPlotWidth()));
  };

  const panViewport = (delta: number) => {
    const currentViewport = normalizePanelViewport(document.viewport);
    const nextRightOffset = clampRightOffset(
      currentViewport.rightOffset + delta,
      currentViewport.visibleCount,
      candles.length
    );
    applyViewport({
      visibleCount: currentViewport.visibleCount,
      rightOffset: nextRightOffset
    });
  };

  const resetViewport = () => {
    runCommand("chart.viewport.set", {
      visibleCount: defaultVisibleBarsForInterval(document.timeframe),
      rightOffset: 0
    });
  };

  const handleWheel = (event: ReactWheelEvent<HTMLCanvasElement>) => {
    event.preventDefault();
    const step = Math.max(12, Math.round(document.viewport.visibleCount * 0.12));
    const delta = event.deltaY > 0 ? step : -step;
    zoomBy(delta);
  };

  const handlePointerDown = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (event.button !== 0) {
      return;
    }
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const rect = event.currentTarget.getBoundingClientRect();
    const point = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    const currentScene = sceneRef.current;
    if (!currentScene) {
      return;
    }

    if (document.interactionState.mode !== "pan") {
      const transform = createCoordinateTransform(currentScene);
      const anchor = transform.pointToAnchor(point.x, point.y, document.symbol);
      if (!anchor) {
        return;
      }
      if (document.interactionState.mode === "select") {
        const hit = hitTestDrawing(currentScene, point.x, point.y);
        if (hit) {
          runCommand("chart.drawing.select", { drawingId: hit.drawing.id });
          drawingDragRef.current = { drawing: hit.drawing, anchor, anchorIndex: hit.anchorIndex };
        } else {
          runCommand("chart.drawing.clearSelection");
        }
        return;
      }

      const drawingType = document.interactionState.mode.replace("draw-", "") as DrawingType;
      if (!drawingNeedsTwoAnchors(drawingType)) {
        runCommand("chart.drawing.add", {
          drawingType,
          anchors: [anchor],
          label: defaultDrawingLabel(drawingType),
          style: defaultDrawingStyle(drawingType, document.interactionState.trendLineExtension)
        });
        return;
      }
      if (drawingDraft?.type === drawingType) {
        runCommand(drawingType === "measurement" ? "chart.measurement.add" : "chart.drawing.add", {
          drawingType,
          anchors: [drawingDraft.first, anchor],
          label: defaultDrawingLabel(drawingType),
          style: defaultDrawingStyle(drawingType, document.interactionState.trendLineExtension)
        });
        setDrawingDraft(null);
        setTransientDrawings(null);
      } else {
        setDrawingDraft({ type: drawingType, first: anchor });
        setTransientDrawings(null);
      }
      return;
    }

    const currentViewport = normalizePanelViewport(document.viewport);
    dragAnchorRef.current = {
      x: event.clientX,
      rightOffset: currentViewport.rightOffset,
      visibleCount: currentViewport.visibleCount
    };
    transientViewportRef.current = currentViewport;
    setTransientViewport(currentViewport);
  };

  const previewViewport = (viewport: ChartViewport) => {
    transientViewportRef.current = viewport;
    setTransientViewport(viewport);
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    setCrosshairPoint({ x: event.clientX - rect.left, y: event.clientY - rect.top });

    const dragAnchor = dragAnchorRef.current;
    const drawingDrag = drawingDragRef.current;
    const currentScene = sceneRef.current;
    if (!currentScene) {
      return;
    }

    if (drawingDrag) {
      const anchor = createCoordinateTransform(currentScene).pointToAnchor(event.clientX - rect.left, event.clientY - rect.top, document.symbol);
      if (!anchor) {
        return;
      }
      const anchors = buildDraggedAnchors(drawingDrag, anchor, currentScene);
      setTransientDrawings(document.drawings.map((drawing) => (
        drawing.id === drawingDrag.drawing.id ? { ...drawing, anchors } : drawing
      )));
      return;
    }

    if (drawingDraft && document.interactionState.mode === `draw-${drawingDraft.type}`) {
      const anchor = createCoordinateTransform(currentScene).pointToAnchor(event.clientX - rect.left, event.clientY - rect.top, document.symbol);
      if (!anchor) {
        setTransientDrawings(null);
        return;
      }
      setTransientDrawings([
        ...document.drawings,
        buildDraftPreviewDrawing(drawingDraft, anchor, document.interactionState.trendLineExtension)
      ]);
      return;
    }

    if (!dragAnchor) {
      return;
    }

    const slotWidth = currentScene.scales.slotWidth;
    previewViewport({
      visibleCount: dragAnchor.visibleCount,
      rightOffset: dragDeltaToRightOffset(
        dragAnchor.rightOffset,
        event.clientX - dragAnchor.x,
        slotWidth,
        dragAnchor.visibleCount,
        candles.length
      )
    });
  };

  const handlePointerUp = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const dragAnchor = dragAnchorRef.current;
    const drawingDrag = drawingDragRef.current;
    const nextViewport = transientViewportRef.current;
    dragAnchorRef.current = null;
    drawingDragRef.current = null;
    transientViewportRef.current = null;
    setTransientViewport(null);
    setTransientDrawings(null);
    try {
      event.currentTarget.releasePointerCapture?.(event.pointerId);
    } catch {
      // Pointer capture can be released by the browser before this handler runs.
    }

    if (drawingDrag) {
      const rect = event.currentTarget.getBoundingClientRect();
      const currentScene = sceneRef.current;
      if (!currentScene) {
        return;
      }
      const anchor = createCoordinateTransform(currentScene).pointToAnchor(event.clientX - rect.left, event.clientY - rect.top, document.symbol);
      if (!anchor) {
        return;
      }
      const anchors = buildDraggedAnchors(drawingDrag, anchor, currentScene);
      runCommand("chart.drawing.update", { drawingId: drawingDrag.drawing.id, drawingPatch: { anchors } });
      return;
    }

    if (!dragAnchor || !nextViewport) {
      return;
    }

    if (nextViewport.rightOffset !== dragAnchor.rightOffset) {
      runCommand("chart.viewport.set", {
        visibleCount: nextViewport.visibleCount,
        rightOffset: nextViewport.rightOffset
      });
    }
  };

  const cancelDrag = () => {
    dragAnchorRef.current = null;
    drawingDragRef.current = null;
    transientViewportRef.current = null;
    setTransientViewport(null);
    setTransientDrawings(null);
  };

  const floatingMenus = typeof window === "undefined" ? null : createPortal(
    <>
      {maMenuOpen && (
        <div
          className="chart-dropdown chart-floating-dropdown chart-ma-menu"
          style={{ top: floatingMenuPosition.top, left: floatingMenuPosition.left }}
          role="menu"
          aria-label="이동평균 표시"
        >
          {movingAverageLayers.map(({ layer, label }) => (
            <label key={layer} className="chart-checkbox-row">
              <input
                type="checkbox"
                checked={document.layers[layer]}
                onChange={() => runCommand("chart.layer.visibility.set", { layer, visible: !document.layers[layer] })}
              />
              <span>{label}</span>
            </label>
          ))}
        </div>
      )}
      {labelEditorOpen && selectedDrawing && (
        <form
          className="chart-dropdown chart-floating-dropdown chart-label-editor"
          style={{ top: floatingMenuPosition.top, left: floatingMenuPosition.left }}
          onSubmit={(event) => {
            event.preventDefault();
            saveSelectedDrawingLabel();
          }}
        >
          <input
            value={labelDraft}
            aria-label="선택한 드로잉 텍스트"
            autoFocus
            onChange={(event) => setLabelDraft(event.target.value)}
          />
          <button type="submit" {...tooltipAttributes("드로잉 텍스트 저장")}>
            <Check size={13} />
          </button>
        </form>
      )}
      {comparisonPickerOpen && (
        <form
          className="chart-dropdown chart-floating-dropdown chart-comparison-picker"
          style={{ top: floatingMenuPosition.top, left: floatingMenuPosition.left }}
          onSubmit={(event) => {
            event.preventDefault();
            submitComparisonDraft();
          }}
        >
          <input
            value={comparisonDraft}
            list={`comparison-symbol-options-${document.id}`}
            placeholder="종목"
            aria-label="비교 종목"
            autoFocus
            onChange={(event) => setComparisonDraft(event.target.value.toUpperCase())}
          />
          <button type="submit" {...tooltipAttributes("비교 추가")}>
            <Check size={13} />
          </button>
          <datalist id={`comparison-symbol-options-${document.id}`}>
            {comparisonOptions.map((symbol) => (
              <option key={symbol} value={symbol} />
            ))}
          </datalist>
          <div className="chart-comparison-options">
            {(comparisonMatches.length ? comparisonMatches : comparisonOptions).map((symbol) => {
              const added = document.comparisons.some((comparison) => comparison.symbol === symbol);
              return (
                <button
                  key={symbol}
                  type="button"
                  className={added ? "active" : ""}
                  aria-pressed={added}
                  aria-label={added ? `${symbol} 비교 제거` : `${symbol} 비교 추가`}
                  onClick={() => toggleComparison(symbol)}
                >
                  {symbol}
                </button>
              );
            })}
            {comparisonOptions.length === 0 && (
              <span className="chart-comparison-empty">검색 결과가 없습니다</span>
            )}
          </div>
        </form>
      )}
      {hoverTooltip && (
        <div
          className={`chart-hover-tooltip ${hoverTooltip.placement}`}
          style={{ top: hoverTooltip.top, left: hoverTooltip.left }}
        >
          {hoverTooltip.label}
        </div>
      )}
    </>,
    window.document.body
  );

  const viewportStep = Math.max(12, Math.round(document.viewport.visibleCount * 0.12));

  return (
    <>
    <div
      ref={panelRef}
      className="chart-panel"
      data-chart-document-id={document.id}
      data-chart-visible-count={document.viewport.visibleCount}
      data-chart-right-offset={document.viewport.rightOffset}
      data-chart-candle-count={candles.length}
      data-chart-comparison-series-count={scene.comparisonSeries.length}
      data-chart-preview-comparison-count={pendingPreview?.comparisons.length ?? 0}
      onPointerMove={updateHoverTooltip}
      onPointerLeave={() => setHoverTooltip(null)}
      onPointerDown={() => setHoverTooltip(null)}
    >
      <div className="chart-toolbar" aria-label="차트 편집 도구">
        <div className="chart-toolbar-scroll">
        <div className="chart-symbol-control" title="화면 범위와 주기">
          <button {...tooltipAttributes("화면 초기화")} onClick={resetViewport}>
            <RefreshCcw size={14} />
          </button>
          <select
            value={document.timeframe}
            aria-label="차트 주기"
            onChange={(event) => runCommand("chart.timeframe.set", { timeframe: event.target.value })}
          >
            {chartIntervals.map((interval) => (
              <option key={interval} value={interval}>{interval}</option>
            ))}
          </select>
        </div>

        <div className="chart-tool-group" aria-label="화면 도구">
          <button {...tooltipAttributes("확대")} onClick={() => zoomBy(-viewportStep)}>
            <ZoomIn size={14} />
          </button>
          <button {...tooltipAttributes("축소")} onClick={() => zoomBy(viewportStep)}>
            <ZoomOut size={14} />
          </button>
          <button {...tooltipAttributes("왼쪽 이동")} onClick={() => panViewport(12)}>
            <MoveLeft size={14} />
          </button>
          <button {...tooltipAttributes("오른쪽 이동")} onClick={() => panViewport(-12)}>
            <MoveRight size={14} />
          </button>
        </div>

        <div className="chart-tool-group" aria-label="레이어 도구">
          {baseLayerControls.map(({ layer, label, icon }) => (
            <button
              key={layer}
              className={document.layers[layer] ? "active chart-layer-button" : "chart-layer-button"}
              {...tooltipAttributes(`${label} 표시`)}
              onClick={() => runCommand("chart.layer.visibility.set", { layer, visible: !document.layers[layer] })}
            >
              <LayerIcon icon={icon} visible={document.layers[layer]} />
            </button>
          ))}
          <div className="chart-popover-anchor">
            <button
              className={hasActiveMovingAverage ? "active chart-layer-button" : "chart-layer-button"}
              {...tooltipAttributes("이동평균")}
              onClick={(event) => {
                placeFloatingMenu(event.currentTarget, 128);
                setMaMenuOpen((open) => !open);
                setLabelEditorOpen(false);
                setComparisonPickerOpen(false);
              }}
            >
              <ChartLine size={14} />
            </button>
          </div>
        </div>

        <div className="chart-tool-group chart-selected-drawing-tools" aria-label="선택한 드로잉 도구">
          <div className="chart-popover-anchor">
            <button
              {...tooltipAttributes("선택한 드로잉 텍스트 수정")}
              disabled={!selectedDrawing}
              onClick={(event) => {
                placeFloatingMenu(event.currentTarget, 190);
                setLabelEditorOpen((open) => !open);
                setMaMenuOpen(false);
                setComparisonPickerOpen(false);
              }}
            >
              <TextCursor size={14} />
            </button>
          </div>
          <button
            {...tooltipAttributes("선택한 드로잉 스타일 수정")}
            disabled={!selectedDrawing}
            onClick={updateSelectedDrawingStyle}
          >
            <Palette size={14} />
          </button>
          <button
            {...tooltipAttributes("선택한 드로잉 삭제")}
            disabled={!selectedDrawing}
            onClick={removeSelectedDrawing}
          >
            <Eraser size={14} />
          </button>
          <button
            {...tooltipAttributes("모든 드로잉 삭제")}
            disabled={document.drawings.length === 0}
            onClick={clearAllDrawings}
          >
            <Trash2 size={14} />
          </button>
        </div>

        </div>

        <div className="chart-right-actions" aria-label="차트 제안 동작">
          <span
            className={`chart-stream-status ${streamStatus}`}
            title={streamMessage ?? `실시간 스트림 상태: ${streamStatusLabel(streamStatus)}`}
            aria-label={`실시간 스트림 상태: ${streamStatusLabel(streamStatus)}`}
          >
            {streamStatusLabel(streamStatus)}
          </span>
          <div className="chart-tool-group chart-comparison-tools" aria-label="비교 도구">
            <div className="chart-popover-anchor">
              <button
                {...tooltipAttributes("비교 추가")}
                onClick={(event) => {
                  placeFloatingMenu(event.currentTarget, 190);
                  setComparisonPickerOpen((open) => !open);
                  setMaMenuOpen(false);
                  setLabelEditorOpen(false);
                }}
              >
                <ComparisonOverlayIcon />
              </button>
            </div>
          </div>
          <div className="chart-tool-group chart-history-tools" aria-label="차트 명령 도구">
            <button {...tooltipAttributes("차트 실행 취소")} disabled={document.history.length === 0} onClick={() => runCommand("chart.undo")}>
              <RotateCcw size={14} />
            </button>
            <button {...tooltipAttributes("차트 다시 실행")} disabled={document.future.length === 0} onClick={() => runCommand("chart.redo")}>
              <RotateCw size={14} />
            </button>
          </div>
          <button
            className="chart-ai-entry-button"
            {...tooltipAttributes("AI에게 묻기")}
            onClick={(event) => {
              event.stopPropagation();
              onAskAgent(panel.id, document.id);
            }}
          >
            <Bot size={14} />
          </button>
          <div className="chart-tool-group chart-preview-actions" aria-label="미리보기 동작">
            <button
              className={[
                "chart-preview-button",
                pendingPreview?.visible ? "active" : "",
                previewPulseKey && previewPulseKey === pendingPreviewKey ? "pulse" : ""
              ].filter(Boolean).join(" ")}
              {...tooltipAttributes(pendingPreview?.visible ? "미리보기 숨기기" : "미리보기 보기")}
              disabled={!pendingPreview}
              onClick={() => runCommand("chart.preview.toggle", { previewVisible: !pendingPreview?.visible })}
            >
              {pendingPreview?.visible ? <Eye size={14} /> : <EyeOff size={14} />}
            </button>
            <button
              className="chart-apply-preview-button"
              {...tooltipAttributes("미리보기 적용")}
              disabled={!pendingPreview?.visible}
              onClick={() => runCommand("chart.preview.apply")}
            >
              <Check size={14} />
            </button>
          </div>
        </div>
      </div>

      <div className="chart-body">
        <div className="chart-drawing-rail" aria-label="드로잉 도구">
          {chartToolRegistry.map((tool) => (
            <div className="chart-tool-slot" key={tool.id}>
              <button
                className={document.interactionState.mode === tool.id ? "active" : ""}
                {...tooltipAttributes(chartToolLabel(tool.id))}
                onClick={() => setDocumentToolMode(tool.id)}
              >
                <DrawingToolIcon toolId={tool.id} />
              </button>
              {tool.id === "draw-trendLine" && document.interactionState.mode === "draw-trendLine" && (
                <div className="chart-trend-extension-menu" aria-label="추세선 모드">
                  {trendLineExtensionOptions.map((option) => (
                    <button
                      key={option.value}
                      className={document.interactionState.trendLineExtension === option.value ? "active" : ""}
                      {...tooltipAttributes(option.label)}
                      onClick={() => setDocumentToolMode("draw-trendLine", option.value, { preserveDraft: true })}
                    >
                      <TrendExtensionIcon extension={option.value} />
                    </button>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>

        <div className="chart-canvas-wrap" ref={canvasWrapRef}>
          <canvas
            ref={canvasRef}
            aria-label={`${document.symbol} 캔들 차트`}
            onWheel={handleWheel}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerLeave={() => {
              if (!dragAnchorRef.current) {
                setCrosshairPoint(undefined);
              }
              if (!dragAnchorRef.current && !drawingDragRef.current) {
                setTransientDrawings(null);
              }
            }}
            onPointerUp={handlePointerUp}
            onPointerCancel={cancelDrag}
            onLostPointerCapture={cancelDrag}
          />
        </div>
      </div>

    </div>
    {floatingMenus}
    </>
  );
}

function LayerIcon({ icon, visible }: { icon: "candle" | "volume"; visible: boolean }) {
  if (!visible) {
    return <EyeOff size={14} />;
  }
  if (icon === "candle") {
    return <ChartCandlestick size={14} />;
  }
  if (icon === "volume") {
    return <BarChart3 size={14} />;
  }
  return <ChartCandlestick size={14} />;
}

function readTooltipTarget(target: EventTarget | null, scope: HTMLElement): HTMLElement | null {
  const element = target instanceof Element ? target.closest<HTMLElement>("[data-tooltip]") : null;
  if (!element || !scope.contains(element)) {
    return null;
  }
  if (element instanceof HTMLButtonElement && element.disabled) {
    return null;
  }
  return element;
}

function ComparisonOverlayIcon() {
  return (
    <svg
      className="chart-comparison-overlay-icon"
      width="16"
      height="16"
      viewBox="0 0 16 16"
      aria-hidden="true"
    >
      <rect className="chart-comparison-back" x="2.2" y="2.2" width="9.2" height="7.2" rx="1.2" />
      <polyline className="chart-comparison-back-line" points="3.5,7.2 5.3,5.6 7.2,6.4 9.8,4.1" />
      <rect x="4.6" y="6.2" width="9.2" height="7.2" rx="1.2" />
      <polyline points="5.9,11.1 7.7,9.7 9.5,10.4 12.2,8.2" />
    </svg>
  );
}

function DrawingToolIcon({ toolId }: { toolId: ChartToolMode }) {
  switch (toolId) {
    case "select":
      return <MousePointer2 size={14} />;
    case "pan":
      return <Hand size={14} />;
    case "draw-horizontalLine":
      return <Minus size={14} />;
    case "draw-trendLine":
      return <TrendToolIcon />;
    case "draw-verticalMarker":
      return <span className="chart-marker-line-icon" aria-hidden="true" />;
    case "draw-textLabel":
      return <Type size={14} />;
    case "draw-pointMarker":
      return <CircleDot size={14} />;
    case "draw-arrow":
      return <ArrowUpRight size={14} />;
    case "draw-rangeBox":
      return <Square size={14} />;
    case "draw-measurement":
      return <Ruler size={14} />;
    default:
      return <MousePointer2 size={14} />;
  }
}

function TrendToolIcon() {
  return (
    <svg className="chart-trend-line-icon" viewBox="0 0 16 16" aria-hidden="true">
      <path d="M3 12 L13 4" />
    </svg>
  );
}

function TrendExtensionIcon({ extension }: { extension: ChartLineExtension }) {
  if (extension === "segment") {
    return (
      <svg className="chart-trend-extension-svg" viewBox="0 0 18 18" aria-hidden="true">
        <path d="M4 14 L14 4" />
        <circle cx="4" cy="14" r="1.8" />
        <circle cx="14" cy="4" r="1.8" />
      </svg>
    );
  }

  if (extension === "ray") {
    return (
      <svg className="chart-trend-extension-svg" viewBox="0 0 18 18" aria-hidden="true">
        <path d="M4 14 L14 4" />
        <circle cx="4" cy="14" r="1.8" />
        <path d="M10.6 4.1 L14 4 L13.9 7.4" />
      </svg>
    );
  }

  return (
    <svg className="chart-trend-extension-svg" viewBox="0 0 18 18" aria-hidden="true">
      <path d="M3 15 L15 3" />
      <path d="M6.3 14.8 L3 15 L3.2 11.7" />
      <path d="M11.7 3.2 L15 3 L14.8 6.3" />
    </svg>
  );
}

function buildDraftPreviewDrawing(
  draft: DrawingDraft,
  anchor: DrawingAnchor,
  trendLineExtension: ChartLineExtension
): DrawingEntity {
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

function defaultDrawingStyle(type: DrawingType, trendLineExtension: ChartLineExtension = "segment") {
  if (type === "rangeBox") {
    return { color: "#2563eb", fillColor: "rgba(37, 99, 235, 0.12)", lineWidth: 1.4 };
  }
  if (type === "trendLine") {
    return { color: "#111111", lineWidth: 1.5, extension: trendLineExtension };
  }
  if (type === "measurement") {
    return { color: "#7c3aed", textColor: "#4c1d95", lineWidth: 1.4 };
  }
  if (type === "arrow") {
    return { color: "#d97706", lineWidth: 1.6 };
  }
  return { color: "#111111", lineWidth: 1.5 };
}

function defaultDrawingLabel(type?: DrawingType) {
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

function comparisonColor(symbol: SupportedSymbol) {
  switch (symbol) {
    case "AAPL":
      return "#2563eb";
    case "MSFT":
      return "#7c3aed";
    case "TSLA":
      return "#dc2626";
    case "SPY":
      return "#0f766e";
    default:
      return "#111111";
  }
}

function buildDraggedAnchors(drag: DrawingDrag, anchor: DrawingAnchor, scene: RenderScene): DrawingAnchor[] {
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

function hitTestDrawing(scene: RenderScene, x: number, y: number): { drawing: DrawingEntity; anchorIndex: number | null } | null {
  const transform = createCoordinateTransform(scene);
  for (const drawing of [...scene.document.drawings].reverse()) {
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

function distance(x1: number, y1: number, x2: number, y2: number) {
  return Math.hypot(x2 - x1, y2 - y1);
}

function distanceToSegment(x: number, y: number, start: { x: number; y: number }, end: { x: number; y: number }) {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared === 0) {
    return distance(x, y, start.x, start.y);
  }
  const t = Math.max(0, Math.min(1, ((x - start.x) * dx + (y - start.y) * dy) / lengthSquared));
  return distance(x, y, start.x + t * dx, start.y + t * dy);
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

function backfillDevLogLevel(status: string): ChartDevLogInput["level"] {
  if (status === "failed" || status === "unavailable") {
    return "error";
  }
  if (status === "queued" || status === "running") {
    return "warn";
  }
  return "info";
}

function backfillStatusMessage(status: string, error?: string): string {
  if (status === "queued" || status === "running") {
    return "Preparing candle data...";
  }
  if (status === "failed") {
    return error || "Historical candle backfill failed.";
  }
  if (status === "unavailable") {
    return error || "Historical candle backfill is unavailable.";
  }
  if (status === "succeeded") {
    return "Backfill completed, but no stored candles were found for this chart.";
  }
  return "No candle data is available for this symbol and interval.";
}

function summarizeBackfillResult(result?: Record<string, unknown>): Record<string, unknown> | undefined {
  if (!result) {
    return undefined;
  }
  return {
    source: result.source,
    rawRowCount: result.rawRowCount,
    processedRowCount: result.processedRowCount,
    materializedRowCount: result.materializedRowCount,
    skippedInvalidRowCount: result.skippedInvalidRowCount,
    materializedSource: result.materializedSource,
    archiveStatus: result.archiveStatus,
    archiveRowCount: result.archiveRowCount,
    archiveObjectCount: result.archiveObjectCount,
    clickhouseCoveredBeforeLoad: result.clickhouseCoveredBeforeLoad,
    skipped: result.skipped,
    reason: result.reason,
    noDataBefore: result.noDataBefore,
    partialHistoryBoundary: result.partialHistoryBoundary,
    missingBeforeCount: result.missingBeforeCount,
    fetchRangeCount: Array.isArray(result.fetchRanges) ? result.fetchRanges.length : undefined,
    gapRangeCount: Array.isArray(result.gapRanges) ? result.gapRanges.length : undefined,
    firstFetchRange: firstRecord(result.fetchRanges),
    lastFetchRange: lastRecord(result.fetchRanges),
    firstGapRange: firstRecord(result.gapRanges),
    lastGapRange: lastRecord(result.gapRanges)
  };
}

function firstRecord(value: unknown): unknown {
  return Array.isArray(value) && value.length ? value[0] : undefined;
}

function lastRecord(value: unknown): unknown {
  return Array.isArray(value) && value.length ? value[value.length - 1] : undefined;
}

function resolveChartSocketUrl(params: URLSearchParams): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const isViteDevServer = window.location.hostname === "127.0.0.1" &&
    (window.location.port === "5173" || window.location.port === "5174");
  const host = isViteDevServer ? "127.0.0.1:8000" : window.location.host;
  return `${protocol}//${host}/ws/charts?${params.toString()}`;
}
