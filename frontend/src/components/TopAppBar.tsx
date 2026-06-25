import { Redo2, Search, Undo2, WandSparkles } from "lucide-react";
import { layoutSnapshotsEqual, makeCommand } from "../layout/commands";
import type { LayoutCommand, SavedLayoutRecord, WorkspaceLayout } from "../layout/types";
import { SystemOrbRail, type AgentOption } from "./SystemArea";

type TopAppBarProps = {
  layout: WorkspaceLayout;
  savedLayouts: SavedLayoutRecord[];
  autoEnabled: boolean;
  agents: AgentOption[];
  selectedAgentIds: string[];
  settingsActive: boolean;
  notificationsActive: boolean;
  onToggleAuto: () => void;
  onToggleNotifications: () => void;
  onToggleAgent: (agentId: string) => void;
  onToggleSettings: () => void;
  onCommand: (command: LayoutCommand) => void;
};

export function TopAppBar({
  layout,
  savedLayouts,
  autoEnabled,
  agents,
  selectedAgentIds,
  settingsActive,
  notificationsActive,
  onToggleAuto,
  onToggleNotifications,
  onToggleAgent,
  onToggleSettings,
  onCommand
}: TopAppBarProps) {
  const favoriteLayouts = [1, 2, 3, 4].map((slot) => savedLayouts.find((record) => record.favoriteSlot === slot));

  return (
    <header className="top-app-bar">
      <label className="brand-search">
        <span className="brand-mark">GOPS</span>
        <span className="search-divider" />
        <input placeholder="Search" />
        <Search size={15} aria-hidden="true" />
      </label>

      <nav className="favorite-layout-strip" aria-label="Favorite layouts">
        {favoriteLayouts.map((layoutRecord, index) => (
          <button
            key={index + 1}
            className={layoutRecord && layoutSnapshotsEqual(layout, layoutRecord.layout)
              ? "favorite-layout-button filled active"
              : layoutRecord
                ? "favorite-layout-button filled"
                : "favorite-layout-button"}
            title={layoutRecord?.name ?? `Favorite layout ${index + 1}`}
            disabled={!layoutRecord}
            onClick={() => {
              if (layoutRecord) {
                onCommand(makeCommand("layout.load", "user", { savedLayoutId: layoutRecord.id }));
              }
            }}
          >
            {index + 1}
          </button>
        ))}
      </nav>

      <div className="top-right-controls" aria-label="Layout controls">
        <div className="toolbar-group" aria-label="Layout history">
          <button title="Layout undo" onClick={() => onCommand(makeCommand("layout.undo", "user"))}>
            <Undo2 size={16} />
          </button>
          <button title="Layout redo" onClick={() => onCommand(makeCommand("layout.redo", "user"))}>
            <Redo2 size={16} />
          </button>
        </div>
        <div className="toolbar-group" aria-label="Automation controls">
          <button
            className={autoEnabled ? "toggle-button active" : "toggle-button"}
            title={autoEnabled ? "AI layout auto apply on" : "AI layout auto apply off"}
            aria-pressed={autoEnabled}
            aria-label="AI layout auto apply"
            onClick={onToggleAuto}
          >
            <WandSparkles size={18} />
          </button>
        </div>
      </div>

      <div className="headline-alert-strip" aria-label="Realtime headline and alert message">
        <span>Realtime headline placeholder</span>
        <strong>Alerts and agent messages appear here</strong>
      </div>

      <SystemOrbRail
        agents={agents}
        selectedAgentIds={selectedAgentIds}
        settingsActive={settingsActive}
        notificationsActive={notificationsActive}
        onToggleAgent={onToggleAgent}
        onToggleNotifications={onToggleNotifications}
        onToggleSettings={onToggleSettings}
      />
    </header>
  );
}
