import { Redo2, Search, Undo2, WandSparkles } from "lucide-react";
import { useEffect, useState } from "react";
import type { SupportedSymbol } from "../chart/symbols";
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
  activeSymbol: SupportedSymbol;
  supportedSymbols: readonly SupportedSymbol[];
  symbolSearchError?: string;
  onToggleAuto: () => void;
  onToggleNotifications: () => void;
  onToggleAgent: (agentId: string) => void;
  onToggleSettings: () => void;
  onSymbolSearch: (symbol: string) => boolean;
  onCommand: (command: LayoutCommand) => void;
};

function isInteractiveTopBarTarget(target: EventTarget | null): boolean {
  const element = target instanceof Element ? target : null;
  return Boolean(element?.closest("button, input, textarea, select, option, datalist, form, a, [role='button']"));
}

export function TopAppBar({
  layout,
  savedLayouts,
  autoEnabled,
  agents,
  selectedAgentIds,
  settingsActive,
  notificationsActive,
  activeSymbol,
  supportedSymbols,
  symbolSearchError,
  onToggleAuto,
  onToggleNotifications,
  onToggleAgent,
  onToggleSettings,
  onSymbolSearch,
  onCommand
}: TopAppBarProps) {
  const favoriteLayouts = [1, 2, 3, 4].map((slot) => savedLayouts.find((record) => record.favoriteSlot === slot));
  const [searchDraft, setSearchDraft] = useState<string>(activeSymbol);

  useEffect(() => {
    setSearchDraft(activeSymbol);
  }, [activeSymbol]);

  return (
    <header
      className="top-app-bar"
      onClick={(event) => {
        if (!isInteractiveTopBarTarget(event.target)) {
          onCommand(makeCommand("layout.panel.select", "user", { clear: true }));
        }
      }}
    >
      <form
        className={symbolSearchError ? "brand-search has-error" : "brand-search"}
        onSubmit={(event) => {
          event.preventDefault();
          onSymbolSearch(searchDraft);
        }}
      >
        <span className="brand-mark">GOPS</span>
        <span className="search-divider" />
        <input
          value={searchDraft}
          list="gops-symbol-options"
          placeholder="Search symbol"
          aria-label="Search symbol"
          aria-invalid={Boolean(symbolSearchError)}
          title={symbolSearchError ?? `Supported symbols: ${supportedSymbols.join(", ")}`}
          onChange={(event) => setSearchDraft(event.target.value.toUpperCase())}
        />
        <button type="submit" className="search-submit-button" title="Search symbol">
          <Search size={15} aria-hidden="true" />
        </button>
        <datalist id="gops-symbol-options">
          {supportedSymbols.map((symbol) => (
            <option key={symbol} value={symbol} />
          ))}
        </datalist>
        {symbolSearchError && <span className="search-error-message">{symbolSearchError}</span>}
      </form>

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
            title={autoEnabled ? "AI command auto apply on" : "AI command auto apply off"}
            aria-pressed={autoEnabled}
            aria-label="AI command auto apply"
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
