import { useState, type DragEvent } from "react";
import type { LayoutCommand, LayoutPreviewItem, PanelPlacement, PanelType, SavedLayoutRecord, WorkspaceLayout } from "../layout/types";
import type { ChartRuntimeAction, ChartRuntimeState } from "../chart/runtime";
import type { SupportedSymbol, WatchlistSymbol } from "../chart/symbols";
import { makeCommand } from "../layout/commands";
import {
  createPanelDropCommand,
  getWorkspaceDropCell,
  PANEL_CATALOG_MIME,
  PANEL_CATALOG_TYPES
} from "../layout/panelCatalogDrop";
import { BoundaryResizeOverlay } from "./BoundaryResizeOverlay";
import { PanelCard } from "./PanelCard";
import { SystemArea, type AgentOption, type SystemMenuTab, type SystemMode } from "./SystemArea";

type WorkspaceGridProps = {
  layout: WorkspaceLayout;
  selectedPanelId?: string;
  systemMode: SystemMode;
  settingsTab: SystemMenuTab;
  agents: AgentOption[];
  selectedAgentIds: string[];
  editingAgentId?: string;
  savedLayouts: SavedLayoutRecord[];
  activeSymbol: SupportedSymbol;
  watchlistSymbols: WatchlistSymbol[];
  chartRuntime: ChartRuntimeState;
  chartAutoApplyEnabled: boolean;
  onSettingsTabChange: (tab: SystemMenuTab) => void;
  onEditAgent: (agentId?: string) => void;
  onUpdateAgent: (agentId: string, patch: Partial<Pick<AgentOption, "label" | "description">>) => void;
  onAddAgent: () => void;
  onDeleteAgent: (agentId: string) => void;
  onCloseSystemPanel: () => void;
  onSelectSymbol: (symbol: string) => boolean;
  onCommand: (command: LayoutCommand) => void;
  onChartAction: (action: ChartRuntimeAction) => void;
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

export function WorkspaceGrid({
  layout,
  selectedPanelId,
  systemMode,
  settingsTab,
  agents,
  selectedAgentIds,
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
  onChartAction
}: WorkspaceGridProps) {
  const [layoutPreview, setLayoutPreview] = useState<LayoutPreviewItem[]>([]);
  const workspacePanels = layout.panels.filter((panel) => panel.placement.group === "workspace");
  const handlePanelCatalogDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (!hasPanelCatalogPayload(event.dataTransfer)) {
      return;
    }

    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  };

  const handlePanelCatalogDrop = (event: DragEvent<HTMLDivElement>) => {
    if (!hasPanelCatalogPayload(event.dataTransfer)) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();

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
        />
      ))}

      {layoutPreview.map((preview) => (
        <div
          key={preview.panelId}
          className="layout-preview-card"
          style={placementStyle(preview.placement)}
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
