import { useState, type DragEvent } from "react";
import type { LayoutCommand, LayoutPreviewItem, PanelPlacement, PanelType, SavedLayoutRecord, WorkspaceLayout } from "../layout/types";
import type { ChartRuntimeAction, ChartRuntimeState } from "../chart/runtime";
import type { AgentChartReference } from "../chart/agentReference";
import type { SupportedSymbol, WatchlistSymbol } from "../chart/symbols";
import { makeCommand } from "../layout/commands";
import {
  createPanelDropCommand,
  createPanelDropPreview,
  getWorkspaceDropCell,
  PANEL_CATALOG_MIME,
  PANEL_CATALOG_TYPES
} from "../layout/panelCatalogDrop";
import { BoundaryResizeOverlay } from "./BoundaryResizeOverlay";
import { PanelCard } from "./PanelCard";
import { SystemArea, type AgentOption, type AgentUpdatePatch, type SystemMenuTab, type SystemMode } from "./SystemArea";

type WorkspaceGridProps = {
  layout: WorkspaceLayout;
  selectedPanelId?: string;
  systemMode: SystemMode;
  settingsTab: SystemMenuTab;
  agents: AgentOption[];
  selectedAgentIds: string[];
  referencedChartTarget?: AgentChartReference;
  editingAgentId?: string;
  savedLayouts: SavedLayoutRecord[];
  activeSymbol: SupportedSymbol;
  watchlistSymbols: WatchlistSymbol[];
  chartRuntime: ChartRuntimeState;
  chartAutoApplyEnabled: boolean;
  onSettingsTabChange: (tab: SystemMenuTab) => void;
  onEditAgent: (agentId?: string) => void;
  onUpdateAgent: (agentId: string, patch: AgentUpdatePatch) => void;
  onAddAgent: () => void;
  onDeleteAgent: (agentId: string) => void;
  onCloseSystemPanel: () => void;
  onSelectSymbol: (symbol: string) => boolean;
  onCommand: (command: LayoutCommand) => void;
  onChartAction: (action: ChartRuntimeAction) => void;
  onAskAgentFromChart: (panelId: string, chartDocumentId: string) => void;
};

function placementStyle(placement: PanelPlacement) {
  return {
    gridColumn: `${placement.col} / span ${placement.colSpan}`,
    gridRow: `${placement.row} / span ${placement.rowSpan}`
  };
}

function hasPanelCatalogPayload(dataTransfer: DataTransfer): boolean {
  const types = Array.from(dataTransfer.types);
  return types.includes(PANEL_CATALOG_MIME) || types.includes("text/plain");
}

function readCatalogPanelType(dataTransfer: DataTransfer): PanelType | null {
  const panelType = (dataTransfer.getData(PANEL_CATALOG_MIME) || dataTransfer.getData("text/plain")) as PanelType;
  return PANEL_CATALOG_TYPES.includes(panelType) ? panelType : null;
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
    label: preview.kind === "blocked" ? preview.reason : preview.kind === "replace" ? "Replace panel" : "Add panel"
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
  chartRuntime,
  chartAutoApplyEnabled,
  onSettingsTabChange,
  onEditAgent,
  onUpdateAgent,
  onAddAgent,
  onDeleteAgent,
  onCloseSystemPanel,
  onSelectSymbol,
  onCommand,
  onChartAction,
  onAskAgentFromChart
}: WorkspaceGridProps) {
  const [layoutPreview, setLayoutPreview] = useState<LayoutPreviewItem[]>([]);
  const workspacePanels = layout.panels.filter((panel) => panel.placement.group === "workspace");
  const handlePanelCatalogDragOver = (event: DragEvent<HTMLDivElement>) => {
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

    const cell = getWorkspaceDropCell(event.currentTarget.getBoundingClientRect(), event.clientX, event.clientY);
    const targetPanelId = findDropTargetPanelId(event.target);
    const preview = createPanelDropPreview({ layout, panelType, activeSymbol, cell, targetPanelId });
    event.dataTransfer.dropEffect = preview?.kind === "blocked" ? "none" : "copy";
    setLayoutPreview(previewItemsForPanelDrop(layout, panelType, activeSymbol, cell, targetPanelId));
  };

  const handlePanelCatalogDrop = (event: DragEvent<HTMLDivElement>) => {
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

    const cell = getWorkspaceDropCell(event.currentTarget.getBoundingClientRect(), event.clientX, event.clientY);
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
      className="layout-frame"
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
          chartAutoApplyEnabled={chartAutoApplyEnabled}
          onChartAction={onChartAction}
          onAskAgentFromChart={onAskAgentFromChart}
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

      <BoundaryResizeOverlay layout={layout} onCommand={onCommand} onPreviewChange={setLayoutPreview} />

      <SystemArea
        mode={systemMode}
        settingsTab={settingsTab}
        layout={layout}
        chartRuntime={chartRuntime}
        chartAutoApplyEnabled={chartAutoApplyEnabled}
        agents={agents}
        selectedAgentIds={selectedAgentIds}
        referencedChartTarget={referencedChartTarget}
        editingAgentId={editingAgentId}
        savedLayouts={savedLayouts}
        activeSymbol={activeSymbol}
        watchlistSymbols={watchlistSymbols}
        onSettingsTabChange={onSettingsTabChange}
        onEditAgent={onEditAgent}
        onUpdateAgent={onUpdateAgent}
        onAddAgent={onAddAgent}
        onDeleteAgent={onDeleteAgent}
        onCloseSystemPanel={onCloseSystemPanel}
        onSelectSymbol={onSelectSymbol}
        onCommand={onCommand}
        onChartAction={onChartAction}
      />
    </div>
  );
}
