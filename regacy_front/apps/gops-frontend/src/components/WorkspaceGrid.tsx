import { useEffect, useState, type CSSProperties, type DragEvent } from "react";
import type { LayoutCommand, LayoutPreviewItem, LayoutProposal, PanelPlacement, PanelType, SavedLayoutRecord, WorkspaceLayout } from "../layout/types";
import type { ChartRuntimeAction, ChartRuntimeState } from "@gops/chart-engine/runtime";
import type { AgentChartReference } from "@gops/chart-engine/agentReference";
import type { HotRankingSymbol, SupportedSymbol, WatchlistSymbol } from "@gops/chart-engine/symbols";
import type { ChartDevLogEntry, ChartDevLogInput } from "../diagnostics/chartDevLog";
import { makeCommand } from "../layout/commands";
import {
  createPanelDropCommand,
  createPanelDropPreview,
  getWorkspaceDropCell,
  PANEL_CATALOG_MIME,
  PANEL_CATALOG_TYPES
} from "../layout/panelCatalogDrop";
import { BoundaryResizeOverlay, type ContinuousGridTracks } from "./BoundaryResizeOverlay";
import { PanelCard } from "./PanelCard";
import { SystemArea, type AgentOption, type AgentUpdatePatch, type SystemMenuTab, type SystemMode } from "./SystemArea";

const SYMBOL_DRAG_MIME = "application/x-gops-symbol";

type WorkspaceGridProps = {
  layout: WorkspaceLayout;
  selectedPanelId?: string;
  systemMode: SystemMode | null;
  settingsTab: SystemMenuTab;
  agents: AgentOption[];
  selectedAgentIds: string[];
  referencedChartTarget?: AgentChartReference;
  editingAgentId?: string;
  savedLayouts: SavedLayoutRecord[];
  activeSymbol: SupportedSymbol;
  watchlistSymbols: WatchlistSymbol[];
  hotRankingSymbols: HotRankingSymbol[];
  knownSymbols: WatchlistSymbol[];
  orderChartSymbols: WatchlistSymbol[];
  symbolOptions: WatchlistSymbol[];
  symbolUniverse: readonly SupportedSymbol[];
  backfillEligibleSymbols: readonly SupportedSymbol[];
  chartRuntime: ChartRuntimeState;
  chartDevLogs: readonly ChartDevLogEntry[];
  chartAutoApplyEnabled: boolean;
  onSettingsTabChange: (tab: SystemMenuTab) => void;
  onEditAgent: (agentId?: string) => void;
  onUpdateAgent: (agentId: string, patch: AgentUpdatePatch) => void;
  onAddAgent: () => void;
  onDeleteAgent: (agentId: string) => void;
  onCloseSystemPanel: () => void;
  onSelectSymbol: (symbol: string) => boolean;
  onSymbolOptionsRequest: (query: string) => void;
  onCommand: (command: LayoutCommand) => void;
  onLayoutProposal: (proposal: LayoutProposal) => void;
  onChartAction: (action: ChartRuntimeAction) => void;
  onChartDevLog: (entry: ChartDevLogInput) => void;
  onAskAgentFromChart: (panelId: string, chartDocumentId: string) => void;
  onToggleWatchlistSymbol: (symbol: string) => void;
};

function placementStyle(placement: PanelPlacement) {
  return {
    gridColumn: `${placement.col} / span ${placement.colSpan}`,
    gridRow: `${placement.row} / span ${placement.rowSpan}`
  };
}

function trackStyle(tracks: ContinuousGridTracks): CSSProperties {
  const style: CSSProperties = {};
  const writableStyle = style as Record<string, string>;
  tracks.columns?.forEach((track, index) => {
    writableStyle[`--frame-col-${index + 1}`] = `${track}px`;
  });
  tracks.rows?.forEach((track, index) => {
    writableStyle[`--frame-row-${index + 1}`] = `${track}px`;
  });
  return style;
}

function hasPanelCatalogPayload(dataTransfer: DataTransfer): boolean {
  const types = Array.from(dataTransfer.types);
  return types.includes(PANEL_CATALOG_MIME) || types.includes("text/plain");
}

function readCatalogPanelType(dataTransfer: DataTransfer): PanelType | null {
  const panelType = (dataTransfer.getData(PANEL_CATALOG_MIME) || dataTransfer.getData("text/plain")) as PanelType;
  return PANEL_CATALOG_TYPES.includes(panelType) ? panelType : null;
}

function readDraggedSymbol(dataTransfer: DataTransfer): string | null {
  const symbol = dataTransfer.getData(SYMBOL_DRAG_MIME);
  return symbol.trim() || null;
}

function findDropTargetPanelId(target: EventTarget | null): string | null {
  const element = target instanceof Element ? target : null;
  return element?.closest<HTMLElement>("[data-panel-id]")?.dataset.panelId ?? null;
}

function previewItemsForPanelDrop(
  layout: WorkspaceLayout,
  panelType: PanelType,
  activeSymbol: SupportedSymbol,
  cell: ReturnType<typeof getWorkspaceDropCell>,
  targetPanelId: string | null
): LayoutPreviewItem[] {
  const preview = createPanelDropPreview({ layout, panelType, activeSymbol, cell, targetPanelId });
  if (!preview?.placement) {
    return [];
  }

  return [{
    panelId: preview.kind === "replace" || preview.kind === "blocked" ? preview.panelId ?? "catalog-drop-preview" : "catalog-drop-preview",
    placement: preview.placement,
    state: preview.kind === "blocked" ? "blocked" : preview.kind === "replace" ? "replace" : "valid",
    label: preview.kind === "blocked" ? preview.reason : preview.kind === "replace" ? "패널 교체" : "패널 추가"
  }];
}

export function WorkspaceGrid({
  layout,
  selectedPanelId,
  systemMode,
  settingsTab,
  agents,
  selectedAgentIds,
  referencedChartTarget,
  editingAgentId,
  savedLayouts,
  activeSymbol,
  watchlistSymbols,
  hotRankingSymbols,
  knownSymbols,
  orderChartSymbols,
  symbolOptions,
  symbolUniverse,
  backfillEligibleSymbols,
  chartRuntime,
  chartDevLogs,
  chartAutoApplyEnabled,
  onSettingsTabChange,
  onEditAgent,
  onUpdateAgent,
  onAddAgent,
  onDeleteAgent,
  onCloseSystemPanel,
  onSelectSymbol,
  onSymbolOptionsRequest,
  onCommand,
  onLayoutProposal,
  onChartAction,
  onChartDevLog,
  onAskAgentFromChart,
  onToggleWatchlistSymbol
}: WorkspaceGridProps) {
  const [layoutPreview, setLayoutPreview] = useState<LayoutPreviewItem[]>([]);
  const [gridTracks, setGridTracks] = useState<ContinuousGridTracks>({});
  const workspacePanels = layout.panels.filter((panel) => panel.placement.group === "workspace");
  const systemPanelOpen = Boolean(systemMode);

  useEffect(() => {
    setGridTracks((current) => current.columns ? { rows: current.rows } : current);
  }, [systemPanelOpen]);

  const handlePanelCatalogDragOver = (event: DragEvent<HTMLDivElement>) => {
    const draggedSymbol = readDraggedSymbol(event.dataTransfer);
    if (draggedSymbol) {
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      setLayoutPreview([]);
      return;
    }

    if (!hasPanelCatalogPayload(event.dataTransfer)) {
      return;
    }

    event.preventDefault();
    const panelType = readCatalogPanelType(event.dataTransfer);
    if (!panelType) {
      setLayoutPreview([]);
      event.dataTransfer.dropEffect = "none";
      return;
    }

    const cell = getWorkspaceDropCell(event.currentTarget.getBoundingClientRect(), event.clientX, event.clientY, systemPanelOpen);
    const targetPanelId = findDropTargetPanelId(event.target);
    const preview = createPanelDropPreview({ layout, panelType, activeSymbol, cell, targetPanelId });
    event.dataTransfer.dropEffect = preview?.kind === "blocked" ? "none" : "copy";
    setLayoutPreview(previewItemsForPanelDrop(layout, panelType, activeSymbol, cell, targetPanelId));
  };

  const handlePanelCatalogDrop = (event: DragEvent<HTMLDivElement>) => {
    const draggedSymbol = readDraggedSymbol(event.dataTransfer);
    if (draggedSymbol) {
      event.preventDefault();
      event.stopPropagation();
      setLayoutPreview([]);
      onSelectSymbol(draggedSymbol);
      return;
    }

    if (!hasPanelCatalogPayload(event.dataTransfer)) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();
    setLayoutPreview([]);

    const panelType = readCatalogPanelType(event.dataTransfer);
    if (!panelType) {
      return;
    }

    const cell = getWorkspaceDropCell(event.currentTarget.getBoundingClientRect(), event.clientX, event.clientY, systemPanelOpen);
    const targetPanelId = findDropTargetPanelId(event.target);
    const command = createPanelDropCommand({
      layout,
      panelType,
      activeSymbol,
      cell,
      targetPanelId
    });

    if (command) {
      onCommand(command);
    }
  };

  return (
    <div
      className={`layout-frame ${systemPanelOpen ? "system-panel-open" : "system-panel-closed"}`}
      style={trackStyle(gridTracks)}
      onDragOver={handlePanelCatalogDragOver}
      onDrop={handlePanelCatalogDrop}
      onDragLeave={(event) => {
        const relatedTarget = event.relatedTarget;
        if (relatedTarget instanceof Node && event.currentTarget.contains(relatedTarget)) {
          return;
        }
        setLayoutPreview([]);
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          onCommand(makeCommand("layout.panel.select", "user", { clear: true }));
        }
      }}
    >
      {workspacePanels.map((panel) => (
        <PanelCard
          key={panel.id}
          layout={layout}
          panel={panel}
          selected={panel.id === selectedPanelId}
          style={placementStyle(panel.placement)}
          onCommand={onCommand}
          onPreviewChange={setLayoutPreview}
          chartRuntime={chartRuntime}
          chartDevLogs={chartDevLogs}
          chartAutoApplyEnabled={chartAutoApplyEnabled}
          activeSymbol={activeSymbol}
          backfillEligibleSymbols={backfillEligibleSymbols}
          knownSymbols={knownSymbols}
          watchlistSymbols={watchlistSymbols}
          hotRankingSymbols={hotRankingSymbols}
          orderChartSymbols={orderChartSymbols}
          symbolOptions={symbolOptions}
          onChartAction={onChartAction}
          onChartDevLog={onChartDevLog}
          onAskAgentFromChart={onAskAgentFromChart}
          onSelectSymbol={onSelectSymbol}
          onSymbolOptionsRequest={onSymbolOptionsRequest}
          onToggleWatchlistSymbol={onToggleWatchlistSymbol}
          systemColumnVisible={systemPanelOpen}
        />
      ))}

      {layoutPreview.map((preview) => (
        <div
          key={preview.panelId}
          className={`layout-preview-card ${preview.state ?? "valid"}`}
          style={placementStyle(preview.placement)}
          title={preview.label}
          aria-hidden="true"
        />
      ))}

      <BoundaryResizeOverlay
        layout={layout}
        tracks={gridTracks}
        onPreviewChange={setLayoutPreview}
        onTrackResize={(axis, tracks) => {
          setGridTracks((current) => axis === "x" ? { ...current, columns: tracks } : { ...current, rows: tracks });
        }}
        systemColumnVisible={systemPanelOpen}
      />

      {systemMode && (
        <SystemArea
          mode={systemMode}
          settingsTab={settingsTab}
          layout={layout}
          chartRuntime={chartRuntime}
          agents={agents}
          selectedAgentIds={selectedAgentIds}
          referencedChartTarget={referencedChartTarget}
          editingAgentId={editingAgentId}
          savedLayouts={savedLayouts}
          activeSymbol={activeSymbol}
          watchlistSymbols={watchlistSymbols}
          symbolUniverse={symbolUniverse}
          onSettingsTabChange={onSettingsTabChange}
          onEditAgent={onEditAgent}
          onUpdateAgent={onUpdateAgent}
          onAddAgent={onAddAgent}
          onDeleteAgent={onDeleteAgent}
          onCloseSystemPanel={onCloseSystemPanel}
          onSelectSymbol={onSelectSymbol}
          onCommand={onCommand}
          onLayoutProposal={onLayoutProposal}
        />
      )}
    </div>
  );
}
