import { Pin, X } from "lucide-react";
import { useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent } from "react";
import { ChartPanel } from "./ChartPanel";
import { getCandlesForDocument, getChartDocumentForPanel, getStreamStatusForDocument, type ChartRuntimeAction, type ChartRuntimeState } from "../chart/runtime";
import { getSymbolMeta } from "../chart/symbols";
import type { ChartDocument } from "../chart/types";
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
  onChartAction: (action: ChartRuntimeAction) => void;
  onAskAgentFromChart: (panelId: string, chartDocumentId: string) => void;
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

function PanelBody({
  panel,
  chartRuntime,
  chartAutoApplyEnabled,
  onChartAction,
  onAskAgentFromChart
}: {
  panel: PanelInstance;
  chartRuntime: ChartRuntimeState;
  chartAutoApplyEnabled: boolean;
  onChartAction: (action: ChartRuntimeAction) => void;
  onAskAgentFromChart: (panelId: string, chartDocumentId: string) => void;
}) {
  if (panel.type === "chart") {
    return (
      <ChartPanel
        panel={panel}
        runtime={chartRuntime}
        autoApplyEnabled={chartAutoApplyEnabled}
        onChartAction={onChartAction}
        onAskAgent={onAskAgentFromChart}
      />
    );
  }

  return (
    <div className="panel-dummy">
      <span>{panel.title ?? panel.type}</span>
      <small>Dummy panel content</small>
    </div>
  );
}

function getGridMetrics(target: EventTarget | null) {
  const element = target instanceof Element ? target : null;
  const frame = element?.closest(".layout-frame");
  if (!frame) {
    return null;
  }

  const rect = frame.getBoundingClientRect();
  return {
    columnStarts: workspaceColumnStarts(rect.width),
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

function changedPanelPreview(current: WorkspaceLayout, next: WorkspaceLayout): LayoutPreviewItem[] {
  return next.panels.flatMap((nextPanel) => {
    const currentPanel = current.panels.find((item) => item.id === nextPanel.id);
    if (!currentPanel || JSON.stringify(currentPanel.placement) === JSON.stringify(nextPanel.placement)) {
      return [];
    }

    return [{ panelId: nextPanel.id, placement: nextPanel.placement }];
  });
}

function resolvePanelHeaderPresentation(panel: PanelInstance, chartRuntime: ChartRuntimeState): PanelHeaderPresentation {
  if (panel.type === "chart") {
    const chartDocument = getChartDocumentForPanel(chartRuntime, panel);
    const symbolMeta = getSymbolMeta(chartDocument.symbol);
    return {
      title: symbolMeta.symbol,
      description: symbolMeta.name,
      market: symbolMeta.market,
      kind: "chart",
      marketMetrics: resolveChartHeaderMetrics(chartRuntime, chartDocument)
    };
  }

  return {
    title: panel.title ?? panel.type,
    description: panelHeaderSubtitle(panel.type)
  };
}

function panelHeaderSubtitle(panelType: PanelInstance["type"]): string {
  switch (panelType) {
    case "watchlist":
      return "Tracked symbols";
    case "newsFeed":
      return "Market news";
    case "proposalReview":
      return "Agent proposals";
    case "symbolSummary":
      return "Symbol snapshot";
    case "indicatorCompare":
      return "Indicator compare";
    case "aiSummary":
      return "AI notes";
    case "notifications":
      return "Alerts";
    case "agentStatus":
      return "Agent status";
    case "agentChat":
      return "Agent chat";
    default:
      return "Workspace panel";
  }
}

function resolveChartHeaderMetrics(chartRuntime: ChartRuntimeState, chartDocument: ChartDocument): PanelMarketMetrics {
  const streamStatus = getStreamStatusForDocument(chartRuntime, chartDocument);
  const offlineMetrics: PanelMarketMetrics = {
    price: "-",
    change: "-",
    direction: "offline"
  };
  if (streamStatus !== "live") {
    return offlineMetrics;
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
  onChartAction,
  onAskAgentFromChart
}: PanelCardProps) {
  const [dragging, setDragging] = useState(false);
  const commandTarget = { panelId: panel.id, group: panel.placement.group, zone: panel.placement.zone };
  const panelHeader = resolvePanelHeaderPresentation(panel, chartRuntime);

  const runPanelCommand = (type: LayoutCommand["type"], payload: Record<string, unknown> = {}) => {
    onCommand(makeCommand(type, "user", { panelId: panel.id, ...payload }, commandTarget));
  };

  const beginDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0 || panel.layoutPinned || panel.placement.group !== "workspace") {
      return;
    }

    const metrics = getGridMetrics(event.currentTarget);
    if (!metrics) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();
    const interactionTarget = event.currentTarget;
    interactionTarget.setPointerCapture?.(event.pointerId);
    setDragging(true);

    const startX = event.clientX;
    const startY = event.clientY;
    const startPlacement = panel.placement;
    let latestX = startX;
    let latestY = startY;
    let finished = false;

    const updatePreview = (clientX: number, clientY: number) => {
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
        onPreviewChange([]);
        return { col: nextCol, row: nextRow };
      }

      const result = applyPanelMoveWithPacking(layout, panel.id, {
        ...startPlacement,
        col: nextCol,
        row: nextRow
      });
      onPreviewChange(result.ok ? changedPanelPreview(layout, result.layout) : []);
      return { col: nextCol, row: nextRow };
    };

    const handlePointerMove = (moveEvent: PointerEvent) => {
      latestX = moveEvent.clientX;
      latestY = moveEvent.clientY;
      updatePreview(latestX, latestY);
    };

    const handlePointerUp = () => {
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
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      setDragging(false);
      onPreviewChange([]);

      const { col: nextCol, row: nextRow } = updatePreview(latestX, latestY);
      onPreviewChange([]);

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

    interactionTarget.addEventListener("pointermove", handlePointerMove);
    interactionTarget.addEventListener("pointerup", handlePointerUp, { once: true });
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp, { once: true });
  };

  return (
    <article
      className={`panel-card ${selected ? "selected" : ""} ${dragging ? "dragging" : ""} ${panel.layoutPinned ? "pinned" : ""}`}
      data-panel-id={panel.id}
      style={style}
      onClick={() => runPanelCommand("layout.panel.select")}
    >
      <header className="panel-header" onPointerDown={beginDrag}>
        <div className={panelHeader.kind === "chart" ? "panel-title-block chart-title-block" : "panel-title-block"}>
          <strong>{panelHeader.title}</strong>
          {panelHeader.kind === "chart" ? (
            <div className="panel-chart-meta">
              <span>{panelHeader.description}</span>
              <em>{panelHeader.market}</em>
            </div>
          ) : (
            <span>{panelHeader.description}</span>
          )}
        </div>
        <div className="panel-actions" onPointerDown={(event) => event.stopPropagation()}>
          {panelHeader.marketMetrics && (
            <div className={`panel-market-metrics ${panelHeader.marketMetrics.direction}`}>
              <strong>{panelHeader.marketMetrics.price}</strong>
              <span>{panelHeader.marketMetrics.change}</span>
            </div>
          )}
          <button
            title={panel.layoutPinned ? "Unpin" : "Pin"}
            aria-pressed={Boolean(panel.layoutPinned)}
            onClick={(event) => {
              event.stopPropagation();
              runPanelCommand(panel.layoutPinned ? "layout.panel.unpin" : "layout.panel.pin");
            }}
          >
            <Pin size={14} fill={panel.layoutPinned ? "currentColor" : "none"} />
          </button>
          <button
            title="Remove"
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
        onChartAction={onChartAction}
        onAskAgentFromChart={onAskAgentFromChart}
      />
    </article>
  );
}
