import { useState } from "react";
import type { LayoutCommand, LayoutPreviewItem, PanelPlacement, SavedLayoutRecord, WorkspaceLayout } from "../layout/types";
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
  onSettingsTabChange: (tab: SystemMenuTab) => void;
  onEditAgent: (agentId?: string) => void;
  onUpdateAgent: (agentId: string, patch: Partial<Pick<AgentOption, "label" | "description">>) => void;
  onAddAgent: () => void;
  onDeleteAgent: (agentId: string) => void;
  onCloseSystemPanel: () => void;
  onCommand: (command: LayoutCommand) => void;
};

function placementStyle(placement: PanelPlacement) {
  return {
    gridColumn: `${placement.col} / span ${placement.colSpan}`,
    gridRow: `${placement.row} / span ${placement.rowSpan}`
  };
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
  onSettingsTabChange,
  onEditAgent,
  onUpdateAgent,
  onAddAgent,
  onDeleteAgent,
  onCloseSystemPanel,
  onCommand
}: WorkspaceGridProps) {
  const [layoutPreview, setLayoutPreview] = useState<LayoutPreviewItem[]>([]);
  const workspacePanels = layout.panels.filter((panel) => panel.placement.group === "workspace");

  return (
    <div className="layout-frame">
      {workspacePanels.map((panel) => (
        <PanelCard
          key={panel.id}
          layout={layout}
          panel={panel}
          selected={panel.id === selectedPanelId}
          style={placementStyle(panel.placement)}
          onCommand={onCommand}
          onPreviewChange={setLayoutPreview}
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
        agents={agents}
        selectedAgentIds={selectedAgentIds}
        editingAgentId={editingAgentId}
        savedLayouts={savedLayouts}
        onSettingsTabChange={onSettingsTabChange}
        onEditAgent={onEditAgent}
        onUpdateAgent={onUpdateAgent}
        onAddAgent={onAddAgent}
        onDeleteAgent={onDeleteAgent}
        onCloseSystemPanel={onCloseSystemPanel}
        onCommand={onCommand}
      />
    </div>
  );
}
