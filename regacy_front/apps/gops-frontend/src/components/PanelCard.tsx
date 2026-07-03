import { ExternalLink, GripVertical, Pin, Star, X } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent } from "react";
import { ChartDevLogPanel } from "./ChartDevLogPanel";
import { ChartPanel } from "./ChartPanel";
import { OrderTicket } from "./OrderTicket";
import { PortfolioHoldingsPanel } from "./PortfolioHoldingsPanel";
import { getCandlesForDocument, getChartDocumentForPanel, type ChartRuntimeAction, type ChartRuntimeState } from "@gops/chart-engine/runtime";
import { getSymbolMeta, normalizeSupportedSymbol, type HotRankingSymbol, type SupportedSymbol, type SymbolMeta, type WatchlistSymbol } from "@gops/chart-engine/symbols";
import type { ChartDocument } from "@gops/chart-engine/types";
import type { ChartDevLogEntry, ChartDevLogInput } from "../diagnostics/chartDevLog";
import { makeCommand } from "../layout/commands";
import { workspaceColumnCount, workspaceColumnStarts, workspaceRowCount, workspaceRowStarts } from "../layout/gridGeometry";
import { applyPanelMoveWithPacking } from "../layout/reflow";
import type { LayoutCommand, LayoutPreviewItem, PanelInstance, WorkspaceLayout } from "../layout/types";

type PanelCardProps = {
  layout: WorkspaceLayout;
  panel: PanelInstance;
  selected: boolean;
  style: CSSProperties;
  onCommand: (command: LayoutCommand) => void;
  onPreviewChange: (preview: LayoutPreviewItem[]) => void;
  chartRuntime: ChartRuntimeState;
  chartAutoApplyEnabled: boolean;
  activeSymbol: SupportedSymbol;
  backfillEligibleSymbols: readonly SupportedSymbol[];
  chartDevLogs: readonly ChartDevLogEntry[];
  knownSymbols: readonly WatchlistSymbol[];
  watchlistSymbols: readonly WatchlistSymbol[];
  hotRankingSymbols: readonly HotRankingSymbol[];
  orderChartSymbols: readonly WatchlistSymbol[];
  symbolOptions: readonly WatchlistSymbol[];
  onChartAction: (action: ChartRuntimeAction) => void;
  onChartDevLog: (entry: ChartDevLogInput) => void;
  onAskAgentFromChart: (panelId: string, chartDocumentId: string) => void;
  onSelectSymbol: (symbol: string) => boolean;
  onSymbolOptionsRequest: (query: string) => void;
  onToggleWatchlistSymbol: (symbol: string) => void;
  systemColumnVisible: boolean;
};

type PanelHeaderPresentation = {
  title: string;
  description: string;
  market?: string;
  kind?: "chart" | "panel";
  marketMetrics?: PanelMarketMetrics;
};

type PanelMarketMetrics = {
  price: string;
  change: string;
  direction: "up" | "down" | "flat" | "offline";
};

const DRAG_START_THRESHOLD_PX = 5;
const PANEL_MOVE_ANIMATION_MS = 540;
const CHART_HEADER_COMPANY_NAMES: Record<string, string> = {
  AAPL: "Apple",
  AMD: "Advanced Micro Devices",
  AMZN: "Amazon",
  ASML: "ASML Holding",
  AVGO: "Broadcom",
  GOOGL: "Alphabet",
  META: "Meta Platforms",
  MSFT: "Microsoft",
  MU: "Micron Technology",
  NVDA: "NVIDIA",
  TSLA: "Tesla",
  TSM: "Taiwan Semiconductor"
};

function panelGeometryKey(panel: PanelInstance, systemColumnVisible: boolean): string {
  const placement = panel.placement;
  return [
    systemColumnVisible ? "system-open" : "system-closed",
    placement.group,
    placement.zone,
    placement.col,
    placement.row,
    placement.colSpan,
    placement.rowSpan
  ].join(":");
}

function isAnimationInFlight(animation: Animation | null): boolean {
  return animation?.playState === "running" || animation?.playState === "paused";
}

function PanelBody({
  panel,
  chartRuntime,
  chartAutoApplyEnabled,
  activeSymbol,
  backfillEligibleSymbols,
  chartDevLogs,
  hotRankingSymbols,
  orderChartSymbols,
  symbolOptions,
  onChartAction,
  onChartDevLog,
  onAskAgentFromChart,
  onSelectSymbol,
  onSymbolOptionsRequest
}: {
  panel: PanelInstance;
  chartRuntime: ChartRuntimeState;
  chartAutoApplyEnabled: boolean;
  activeSymbol: SupportedSymbol;
  backfillEligibleSymbols: readonly SupportedSymbol[];
  chartDevLogs: readonly ChartDevLogEntry[];
  hotRankingSymbols: readonly HotRankingSymbol[];
  orderChartSymbols: readonly WatchlistSymbol[];
  symbolOptions: readonly WatchlistSymbol[];
  onChartAction: (action: ChartRuntimeAction) => void;
  onChartDevLog: (entry: ChartDevLogInput) => void;
  onAskAgentFromChart: (panelId: string, chartDocumentId: string) => void;
  onSelectSymbol: (symbol: string) => boolean;
  onSymbolOptionsRequest: (query: string) => void;
}) {
  if (panel.type === "chart") {
    return (
      <ChartPanel
        panel={panel}
        runtime={chartRuntime}
        autoApplyEnabled={chartAutoApplyEnabled}
        backfillEligibleSymbols={backfillEligibleSymbols}
        onChartAction={onChartAction}
        onDevLog={onChartDevLog}
        onAskAgent={onAskAgentFromChart}
      />
    );
  }

  if (panel.type === "chartDevLog") {
    return <ChartDevLogPanel entries={chartDevLogs} chartRuntime={chartRuntime} />;
  }

  if (panel.type === "orderTicket") {
    return (
      <OrderTicket
        activeSymbol={activeSymbol}
        chartSymbols={orderChartSymbols}
        symbolOptions={symbolOptions}
        onSymbolOptionsRequest={onSymbolOptionsRequest}
      />
    );
  }

  if (panel.type === "portfolioHoldings") {
    return <PortfolioHoldingsPanel onSelectSymbol={onSelectSymbol} />;
  }

  if (panel.type === "hotRanking") {
    return <HotRankingPanel activeSymbol={activeSymbol} symbols={hotRankingSymbols} onSelectSymbol={onSelectSymbol} />;
  }

  if (panel.type === "newsFeed") {
    return <EmbeddedNewsFeed panel={panel} activeSymbol={activeSymbol} />;
  }

  return (
    <div className="panel-placeholder">
      <small>준비 중인 패널입니다</small>
    </div>
  );
}

type NewsPanelItem = {
  title: string;
  summary?: string;
  localizedTitle?: string;
  localizedSummary?: string;
  originalTitle?: string;
  originalSummary?: string;
  url?: string;
  source?: string;
  publishedAt?: string;
  symbol?: string;
  symbols: string[];
  eventType?: string;
  impactDirection?: string;
  relevanceScore?: number;
  importanceScore?: number;
};

function EmbeddedNewsFeed({
  panel,
  activeSymbol
}: {
  panel: PanelInstance;
  activeSymbol: SupportedSymbol;
}) {
  const [mode, setMode] = useState<"latest" | "major">("latest");
  const latestNews = readNewsItems(panel.props.latestNews);
  const majorNews = readNewsItems(panel.props.majorNews);
  const items = mode === "latest" ? latestNews : majorNews;
  const panelSymbol = readString(panel.props.symbol) ?? activeSymbol;

  if (!latestNews.length && !majorNews.length) {
    return (
      <div className="panel-placeholder panel-placeholder-muted">
        <small>{panelSymbol} 뉴스 분석을 실행하면 주요 뉴스가 표시됩니다</small>
      </div>
    );
  }

  return (
    <div className="panel-news-feed" aria-label={`${panelSymbol} 뉴스`}>
      <div className="panel-news-toolbar" role="tablist" aria-label="뉴스 보기">
        <button className={mode === "latest" ? "active" : ""} type="button" onClick={() => setMode("latest")}>
          최신뉴스
        </button>
        <button className={mode === "major" ? "active" : ""} type="button" onClick={() => setMode("major")}>
          주요뉴스
        </button>
      </div>
      <div className="panel-news-list">
        {items.map((item, index) => (
          <article key={`${item.url ?? item.title}-${index}`} className="panel-news-row">
            <div className="panel-news-row-main">
              {item.url ? (
                <a href={item.url} target="_blank" rel="noreferrer" title={item.originalTitle ?? item.title}>
                  {item.title}
                  <ExternalLink size={12} aria-hidden="true" />
                </a>
              ) : (
                <strong>{item.title}</strong>
              )}
              {item.summary && <p>{item.summary}</p>}
            </div>
            <div className="panel-news-meta">
              <span>{item.symbol ?? panelSymbol}</span>
              <span className={`news-impact ${item.impactDirection ?? "unknown"}`}>{impactDirectionText(item.impactDirection)}</span>
              <span>{item.source ?? "news"}</span>
              {item.publishedAt && <span>{relativeTimeText(item.publishedAt)}</span>}
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}

function readNewsItems(value: unknown): NewsPanelItem[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const items: NewsPanelItem[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      continue;
    }
    const source = item as Record<string, unknown>;
    const title = readString(source.title);
    if (!title) {
      continue;
    }
    items.push({
      title,
      summary: readString(source.summary) ?? undefined,
      localizedTitle: readString(source.localizedTitle) ?? undefined,
      localizedSummary: readString(source.localizedSummary) ?? undefined,
      originalTitle: readString(source.originalTitle) ?? undefined,
      originalSummary: readString(source.originalSummary) ?? undefined,
      url: readString(source.url) ?? undefined,
      source: readString(source.source) ?? undefined,
      publishedAt: readString(source.publishedAt) ?? undefined,
      symbol: readString(source.symbol) ?? undefined,
      symbols: Array.isArray(source.symbols) ? source.symbols.map(readString).filter((symbol): symbol is string => Boolean(symbol)) : [],
      eventType: readString(source.eventType) ?? undefined,
      impactDirection: readString(source.impactDirection) ?? undefined,
      relevanceScore: readNumber(source.relevanceScore) ?? undefined,
      importanceScore: readNumber(source.importanceScore) ?? undefined
    });
  }
  return items;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function impactDirectionText(value?: string): string {
  switch (value) {
    case "positive":
      return "긍정";
    case "negative":
      return "부정";
    case "mixed":
      return "혼재";
    default:
      return "보류";
  }
}

function relativeTimeText(value: string): string {
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    return value.slice(0, 10);
  }
  const diffMinutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60000));
  if (diffMinutes < 60) {
    return `${diffMinutes}분 전`;
  }
  const diffHours = Math.floor(diffMinutes / 60);
  if (diffHours < 24) {
    return `${diffHours}시간 전`;
  }
  return `${Math.floor(diffHours / 24)}일 전`;
}

function getGridMetrics(target: EventTarget | null, systemColumnVisible: boolean) {
  const element = target instanceof Element ? target : null;
  const frame = element?.closest(".layout-frame");
  if (!frame) {
    return null;
  }

  const rect = frame.getBoundingClientRect();
  return {
    columnStarts: workspaceColumnStarts(rect.width, systemColumnVisible),
    rowStarts: workspaceRowStarts(rect.height)
  };
}

function nearestStartIndex(starts: number[], value: number, maxIndex: number): number {
  let bestIndex = 0;
  let bestDistance = Number.POSITIVE_INFINITY;

  for (let index = 0; index <= maxIndex; index += 1) {
    const distance = Math.abs(starts[index] - value);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestIndex = index;
    }
  }

  return bestIndex;
}

function resolvePanelHeaderPresentation(
  panel: PanelInstance,
  chartRuntime: ChartRuntimeState,
  knownSymbols: readonly WatchlistSymbol[]
): PanelHeaderPresentation {
  if (panel.type === "chart") {
    const chartDocument = getChartDocumentForPanel(chartRuntime, panel);
    const normalizedSymbol = normalizeSupportedSymbol(chartDocument.symbol);
    const symbolMeta = knownSymbols.find((item) => item.symbol === normalizedSymbol) ?? getSymbolMeta(chartDocument.symbol);
    return {
      title: symbolMeta.symbol,
      description: resolveChartHeaderCompanyName(symbolMeta),
      market: symbolMeta.market,
      kind: "chart",
      marketMetrics: resolveChartHeaderMetrics(chartRuntime, chartDocument, symbolMeta)
    };
  }

  return {
    title: panel.title ?? panel.type,
    description: panelHeaderSubtitle(panel.type)
  };
}

function resolveChartHeaderCompanyName(symbolMeta: Pick<SymbolMeta, "symbol" | "name">): string {
  const companyName = symbolMeta.name.trim();
  if (companyName && companyName.toUpperCase() !== symbolMeta.symbol) {
    return companyName;
  }

  return CHART_HEADER_COMPANY_NAMES[symbolMeta.symbol] ?? "";
}

function panelHeaderSubtitle(panelType: PanelInstance["type"]): string {
  switch (panelType) {
    case "hotRanking":
      return "거래대금 Top 10";
    case "newsFeed":
      return "시장 뉴스";
    case "indicatorCompare":
      return "지표 비교";
    case "orderTicket":
      return "주문 입력";
    case "portfolioHoldings":
      return "모의투자 보유종목";
    case "aiSummary":
      return "AI 요약";
    case "ontologyGraph":
      return "기업 관계";
    case "chartDevLog":
      return "차트 진단 로그";
    default:
      return "작업 패널";
  }
}

function resolveChartHeaderMetrics(
  chartRuntime: ChartRuntimeState,
  chartDocument: ChartDocument,
  quote?: WatchlistSymbol
): PanelMarketMetrics {
  const offlineMetrics: PanelMarketMetrics = {
    price: "-",
    change: "-",
    direction: "offline"
  };

  if (typeof quote?.lastPrice === "number" && typeof quote.changePercent === "number") {
    return {
      price: quote.lastPrice.toFixed(2),
      change: `${quote.changePercent >= 0 ? "+" : ""}${quote.changePercent.toFixed(2)}%`,
      direction: quote.changePercent > 0 ? "up" : quote.changePercent < 0 ? "down" : "flat"
    };
  }

  const candles = getCandlesForDocument(chartRuntime, chartDocument);
  const visibleEnd = Math.max(0, candles.length - Math.max(0, chartDocument.viewport.rightOffset));
  const visibleStart = Math.max(0, visibleEnd - Math.max(1, chartDocument.viewport.visibleCount));
  const visibleCandles = candles.slice(visibleStart, visibleEnd);
  const first = visibleCandles[0];
  const last = visibleCandles[visibleCandles.length - 1];
  if (!first || !last) {
    return offlineMetrics;
  }

  const changePercent = ((last.close - first.open) / Math.max(0.0001, first.open)) * 100;
  return {
    price: last.close.toFixed(2),
    change: `${changePercent >= 0 ? "+" : ""}${changePercent.toFixed(2)}%`,
    direction: changePercent > 0 ? "up" : changePercent < 0 ? "down" : "flat"
  };
}

export function PanelCard({
  layout,
  panel,
  selected,
  style,
  onCommand,
  onPreviewChange,
  chartRuntime,
  chartAutoApplyEnabled,
  activeSymbol,
  backfillEligibleSymbols,
  chartDevLogs,
  knownSymbols,
  watchlistSymbols,
  hotRankingSymbols,
  orderChartSymbols,
  symbolOptions,
  onChartAction,
  onChartDevLog,
  onAskAgentFromChart,
  onSelectSymbol,
  onSymbolOptionsRequest,
  onToggleWatchlistSymbol,
  systemColumnVisible
}: PanelCardProps) {
  const [dragging, setDragging] = useState(false);
  const [dragOffset, setDragOffset] = useState({ x: 0, y: 0 });
  const panelRef = useRef<HTMLElement | null>(null);
  const previousRectRef = useRef<DOMRect | null>(null);
  const previousGeometryKeyRef = useRef<string | null>(null);
  const movementAnimationRef = useRef<Animation | null>(null);
  const commandTarget = { panelId: panel.id, group: panel.placement.group, zone: panel.placement.zone };
  const geometryKey = panelGeometryKey(panel, systemColumnVisible);
  const panelHeader = resolvePanelHeaderPresentation(panel, chartRuntime, knownSymbols);
  const chartDocument = panel.type === "chart" ? getChartDocumentForPanel(chartRuntime, panel) : null;
  const chartSymbol = chartDocument ? normalizeSupportedSymbol(chartDocument.symbol) : null;
  const chartIsInWatchlist = Boolean(chartSymbol && watchlistSymbols.some((item) => item.symbol === chartSymbol));

  const runPanelCommand = (type: LayoutCommand["type"], payload: Record<string, unknown> = {}) => {
    onCommand(makeCommand(type, "user", { panelId: panel.id, ...payload }, commandTarget));
  };

  const beginDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0 || panel.layoutPinned || panel.placement.group !== "workspace") {
      return;
    }

    const metrics = getGridMetrics(event.currentTarget, systemColumnVisible);
    if (!metrics) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();
    const interactionTarget = event.currentTarget;
    interactionTarget.setPointerCapture?.(event.pointerId);
    setDragOffset({ x: 0, y: 0 });

    const startX = event.clientX;
    const startY = event.clientY;
    const startPlacement = panel.placement;
    let latestX = startX;
    let latestY = startY;
    let finished = false;
    let dragStarted = false;

    const resolveTargetCell = (clientX: number, clientY: number) => {
      const startColPx = metrics.columnStarts[startPlacement.col - 1];
      const startRowPx = metrics.rowStarts[startPlacement.row - 1];
      const nextCol = nearestStartIndex(
        metrics.columnStarts,
        startColPx + clientX - startX,
        workspaceColumnCount - startPlacement.colSpan
      ) + 1;
      const nextRow = nearestStartIndex(
        metrics.rowStarts,
        startRowPx + clientY - startY,
        workspaceRowCount - startPlacement.rowSpan
      ) + 1;

      if (nextCol === startPlacement.col && nextRow === startPlacement.row) {
        return { col: nextCol, row: nextRow };
      }

      const result = applyPanelMoveWithPacking(layout, panel.id, {
        ...startPlacement,
        col: nextCol,
        row: nextRow
      });
      return result.ok ? { col: nextCol, row: nextRow } : { col: startPlacement.col, row: startPlacement.row };
    };

    const handlePointerMove = (moveEvent: PointerEvent) => {
      latestX = moveEvent.clientX;
      latestY = moveEvent.clientY;
      const nextOffset = { x: latestX - startX, y: latestY - startY };

      if (!dragStarted) {
        const distance = Math.hypot(nextOffset.x, nextOffset.y);
        if (distance < DRAG_START_THRESHOLD_PX) {
          return;
        }
        dragStarted = true;
        setDragging(true);
      }

      moveEvent.preventDefault();
      setDragOffset(nextOffset);
    };

    const finishPointerInteraction = (commitMove: boolean) => {
      if (finished) {
        return;
      }
      finished = true;

      try {
        interactionTarget.releasePointerCapture?.(event.pointerId);
      } catch {
        // Capture may already be released by the browser.
      }

      interactionTarget.removeEventListener("pointermove", handlePointerMove);
      interactionTarget.removeEventListener("pointerup", handlePointerUp);
      interactionTarget.removeEventListener("pointercancel", handlePointerCancel);
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      window.removeEventListener("pointercancel", handlePointerCancel);
      setDragging(false);
      setDragOffset({ x: 0, y: 0 });
      onPreviewChange([]);

      if (!commitMove || !dragStarted) {
        return;
      }

      const { col: nextCol, row: nextRow } = resolveTargetCell(latestX, latestY);

      if (nextCol === startPlacement.col && nextRow === startPlacement.row) {
        return;
      }

      runPanelCommand("layout.panel.move", {
        placement: {
          ...startPlacement,
          col: nextCol,
          row: nextRow
        }
      });
    };

    const handlePointerUp = () => finishPointerInteraction(true);
    const handlePointerCancel = () => finishPointerInteraction(false);

    interactionTarget.addEventListener("pointermove", handlePointerMove);
    interactionTarget.addEventListener("pointerup", handlePointerUp, { once: true });
    interactionTarget.addEventListener("pointercancel", handlePointerCancel, { once: true });
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp, { once: true });
    window.addEventListener("pointercancel", handlePointerCancel, { once: true });
  };

  useLayoutEffect(() => {
    const element = panelRef.current;
    if (!element) {
      return;
    }

    const visualRect = element.getBoundingClientRect();
    let nextRect = visualRect;
    const previousRect = previousRectRef.current;
    const previousGeometryKey = previousGeometryKeyRef.current;
    const frameIsResizing = element.closest(".layout-frame")?.classList.contains("resizing-grid") ?? false;
    if (dragging) {
      return;
    }

    const geometryChanged = Boolean(previousGeometryKey && previousGeometryKey !== geometryKey);
    const animationInFlight = isAnimationInFlight(movementAnimationRef.current);
    const animationStartRect = animationInFlight ? visualRect : previousRect;

    if (geometryChanged && animationInFlight) {
      movementAnimationRef.current?.cancel();
      movementAnimationRef.current = null;
      nextRect = element.getBoundingClientRect();
    }

    if (!geometryChanged && animationInFlight) {
      return;
    }

    previousRectRef.current = nextRect;
    previousGeometryKeyRef.current = geometryKey;

    if (frameIsResizing || !animationStartRect || !geometryChanged) {
      return;
    }

    const deltaX = animationStartRect.left - nextRect.left;
    const deltaY = animationStartRect.top - nextRect.top;
    const deltaWidth = animationStartRect.width - nextRect.width;
    const deltaHeight = animationStartRect.height - nextRect.height;
    const shouldAnimate =
      Math.abs(deltaX) > 0.5 ||
      Math.abs(deltaY) > 0.5 ||
      Math.abs(deltaWidth) > 0.5 ||
      Math.abs(deltaHeight) > 0.5;

    if (!shouldAnimate) {
      return;
    }

    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    if (reduceMotion) {
      return;
    }

    const clipRight = Math.max(0, nextRect.width - animationStartRect.width);
    const clipBottom = Math.max(0, nextRect.height - animationStartRect.height);
    const hasResize = Math.abs(deltaWidth) > 0.5 || Math.abs(deltaHeight) > 0.5;
    const startClip = hasResize ? `inset(0 ${clipRight}px ${clipBottom}px 0 round 18px)` : "inset(0 0 0 0 round 18px)";

    movementAnimationRef.current?.cancel();
    const movementAnimation = element.animate(
      [
        {
          transformOrigin: "top left",
          transform: `translate3d(${deltaX}px, ${deltaY}px, 0)`,
          clipPath: startClip,
          opacity: 0.98
        },
        {
          transformOrigin: "top left",
          transform: "translate3d(0, 0, 0)",
          clipPath: "inset(0 0 0 0 round 18px)",
          opacity: 1
        }
      ],
      {
        duration: PANEL_MOVE_ANIMATION_MS,
        easing: "cubic-bezier(0.18, 0.82, 0.18, 1)",
        fill: "both"
      }
    );
    movementAnimationRef.current = movementAnimation;
    movementAnimation.onfinish = () => {
      if (movementAnimationRef.current === movementAnimation) {
        movementAnimationRef.current = null;
      }
    };
    movementAnimation.oncancel = () => {
      if (movementAnimationRef.current === movementAnimation) {
        movementAnimationRef.current = null;
      }
    };
  });

  useLayoutEffect(() => {
    return () => movementAnimationRef.current?.cancel();
  }, []);

  const panelStyle: CSSProperties = dragging
    ? {
        ...style,
        zIndex: 80,
        transform: `translate3d(${dragOffset.x}px, ${dragOffset.y}px, 0)`
      }
    : style;

  return (
    <article
      ref={panelRef}
      className={`panel-card ${selected ? "selected" : ""} ${dragging ? "dragging" : ""} ${panel.layoutPinned ? "pinned" : ""}`}
      data-panel-id={panel.id}
      data-panel-type={panel.type}
      data-panel-row={panel.placement.row}
      data-panel-end-row={panel.placement.row + panel.placement.rowSpan - 1}
      style={panelStyle}
      onClick={() => runPanelCommand("layout.panel.select")}
    >
      <header className="panel-header">
        <button
          type="button"
          className="panel-drag-handle"
          disabled={Boolean(panel.layoutPinned) || panel.placement.group !== "workspace"}
          title={panel.layoutPinned ? "고정된 패널" : "패널 이동"}
          aria-label={panel.layoutPinned ? "고정된 패널" : "패널 이동"}
          onPointerDown={beginDrag}
          onClick={(event) => event.stopPropagation()}
        >
          <GripVertical size={15} />
        </button>
        <div className={panelHeader.kind === "chart" ? "panel-title-block chart-title-block" : "panel-title-block"}>
          {panelHeader.kind === "chart" ? (
            <>
              <strong>{panelHeader.title}</strong>
              {panelHeader.description && (
                <div className="panel-chart-meta">
                  <span>{panelHeader.description}</span>
                </div>
              )}
            </>
          ) : (
            <span>{panelHeader.description}</span>
          )}
        </div>
        {panelHeader.marketMetrics && (
          <div className={`panel-market-metrics panel-market-metrics-static ${panelHeader.marketMetrics.direction}`}>
            <strong>{panelHeader.marketMetrics.price}</strong>
            <span>{panelHeader.marketMetrics.change}</span>
          </div>
        )}
        <div className="panel-actions" onPointerDown={(event) => event.stopPropagation()}>
          {chartSymbol && (
            <button
              className={chartIsInWatchlist ? "panel-watchlist-star active" : "panel-watchlist-star"}
              title={chartIsInWatchlist ? `${chartSymbol} 관심 종목 제거` : `${chartSymbol} 관심 종목 추가`}
              aria-pressed={chartIsInWatchlist}
              aria-label={chartIsInWatchlist ? `${chartSymbol} 관심 종목 제거` : `${chartSymbol} 관심 종목 추가`}
              onClick={(event) => {
                event.stopPropagation();
                onToggleWatchlistSymbol(chartSymbol);
              }}
            >
              <Star size={15} fill={chartIsInWatchlist ? "currentColor" : "none"} />
            </button>
          )}
          <button
            title={panel.layoutPinned ? "고정 해제" : "패널 고정"}
            aria-pressed={Boolean(panel.layoutPinned)}
            onClick={(event) => {
              event.stopPropagation();
              runPanelCommand(panel.layoutPinned ? "layout.panel.unpin" : "layout.panel.pin");
            }}
          >
            <Pin size={14} fill={panel.layoutPinned ? "currentColor" : "none"} />
          </button>
          <button
            title="패널 제거"
            onClick={(event) => {
              event.stopPropagation();
              runPanelCommand("layout.panel.remove");
            }}
          >
            <X size={15} />
          </button>
        </div>
      </header>

      <PanelBody
        panel={panel}
        chartRuntime={chartRuntime}
        chartAutoApplyEnabled={chartAutoApplyEnabled}
        activeSymbol={activeSymbol}
        backfillEligibleSymbols={backfillEligibleSymbols}
        chartDevLogs={chartDevLogs}
        hotRankingSymbols={hotRankingSymbols}
        orderChartSymbols={orderChartSymbols}
        symbolOptions={symbolOptions}
        onChartAction={onChartAction}
        onChartDevLog={onChartDevLog}
        onAskAgentFromChart={onAskAgentFromChart}
        onSelectSymbol={onSelectSymbol}
        onSymbolOptionsRequest={onSymbolOptionsRequest}
      />
    </article>
  );
}

function HotRankingPanel({
  activeSymbol,
  symbols,
  onSelectSymbol
}: {
  activeSymbol: SupportedSymbol;
  symbols: readonly HotRankingSymbol[];
  onSelectSymbol: (symbol: string) => boolean;
}) {
  return (
    <div className="hot-ranking-panel">
      <div className="watchlist-list">
        {symbols.length === 0 && (
          <div className="watchlist-empty">거래대금 순위를 불러오지 못했습니다</div>
        )}
        {symbols.map((item) => (
          <button
            key={`${item.rank}-${item.symbol}`}
            className={item.symbol === activeSymbol ? "watchlist-row active hot-ranking-row" : "watchlist-row hot-ranking-row"}
            data-symbol={item.symbol}
            aria-label={`${item.rank}위 ${item.symbol} 차트 열기`}
            title={`${item.symbol} 차트 열기`}
            onClick={() => onSelectSymbol(item.symbol)}
          >
            <span className="hot-rank-cell">#{item.rank}</span>
            <span className="watchlist-symbol-cell">
              <strong>{item.symbol}</strong>
              <em>{item.name}</em>
            </span>
            <span className="watchlist-quote-cell">
              <strong className={typeof item.changePercent === "number" ? (item.changePercent < 0 ? "market-down" : "market-up") : "watchlist-change-empty"}>
                {typeof item.changePercent === "number" ? `${item.changePercent >= 0 ? "+" : ""}${item.changePercent.toFixed(2)}%` : "-"}
              </strong>
              <em>
                {typeof item.sessionDollarVolume === "number" ? compactDollarVolume(item.sessionDollarVolume) : "거래대금 없음"}
              </em>
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

function compactDollarVolume(value: number): string {
  if (value >= 1_000_000_000) {
    return `$${(value / 1_000_000_000).toFixed(1)}B`;
  }
  if (value >= 1_000_000) {
    return `$${(value / 1_000_000).toFixed(1)}M`;
  }
  if (value >= 1_000) {
    return `$${(value / 1_000).toFixed(1)}K`;
  }
  return `$${value.toFixed(0)}`;
}
