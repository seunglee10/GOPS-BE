import { Bell, Menu, Plus, RotateCcw, Star, Trash2, X } from "lucide-react";
import { MAX_USER_LAYOUTS, layoutSnapshotsEqual, makeCommand } from "../layout/commands";
import type { FavoriteLayoutSlot, LayoutCommand, SavedLayoutRecord, WorkspaceLayout } from "../layout/types";

export type SystemMode = "watchlist" | "settings" | "agents" | "notifications";

export type SystemMenuTab = "layouts" | "agent" | "menu";

export type AgentOption = {
  id: string;
  label: string;
  description: string;
  iconUrl: string;
};

export const initialAgentOptions: AgentOption[] = [
  { id: "agent-01", label: "Agent 01", description: "Layout and market assistant.", iconUrl: "/assets/agent-icons/agent-01.svg" },
  { id: "agent-02", label: "Agent 02", description: "News and context assistant.", iconUrl: "/assets/agent-icons/agent-02.svg" },
  { id: "agent-03", label: "Agent 03", description: "Signal review assistant.", iconUrl: "/assets/agent-icons/agent-03.svg" },
  { id: "agent-04", label: "Agent 04", description: "Portfolio watch assistant.", iconUrl: "/assets/agent-icons/agent-04.svg" }
];

const orchestratorAgent: AgentOption = {
  id: "orchestrator",
  label: "Orchestrator",
  description: "Automatically coordinates multi-agent mode.",
  iconUrl: "/assets/agent-icons/agent-12.svg"
};

type SystemAreaProps = {
  mode: SystemMode;
  settingsTab: SystemMenuTab;
  layout: WorkspaceLayout;
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

type SettingsPanelProps = {
  settingsTab: SystemMenuTab;
  layout: WorkspaceLayout;
  agents: AgentOption[];
  editingAgentId?: string;
  savedLayouts: SavedLayoutRecord[];
  onSettingsTabChange: (tab: SystemMenuTab) => void;
  onEditAgent: (agentId?: string) => void;
  onUpdateAgent: (agentId: string, patch: Partial<Pick<AgentOption, "label" | "description">>) => void;
  onAddAgent: () => void;
  onDeleteAgent: (agentId: string) => void;
  onCommand: (command: LayoutCommand) => void;
};

export function SystemArea({
  mode,
  settingsTab,
  layout,
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
}: SystemAreaProps) {
  const selectedAgents = agents.filter((agent) => selectedAgentIds.includes(agent.id));
  const activeAgents = selectedAgents.length > 1 ? [orchestratorAgent, ...selectedAgents] : selectedAgents;
  const agentHeaderTitle = selectedAgents.length > 1
    ? "Orchestration"
    : selectedAgents[0]?.label ?? "LLM Agent";
  const agentHeaderDetail = selectedAgents.length > 1
    ? selectedAgents.map((agent) => agent.label).join(" / ")
    : selectedAgents[0]?.description ?? "Select an agent";

  return (
    <aside className="system-area" aria-label="System area">
      {mode !== "watchlist" && (
        <button className="system-panel-close" title="Close system panel" onClick={onCloseSystemPanel}>
          <X size={16} />
        </button>
      )}

      {mode === "settings" && (
        <SettingsPanel
          settingsTab={settingsTab}
          layout={layout}
          agents={agents}
          editingAgentId={editingAgentId}
          savedLayouts={savedLayouts}
          onSettingsTabChange={onSettingsTabChange}
          onEditAgent={onEditAgent}
          onUpdateAgent={onUpdateAgent}
          onAddAgent={onAddAgent}
          onDeleteAgent={onDeleteAgent}
          onCommand={onCommand}
        />
      )}

      {mode === "agents" && (
        <div className="system-mode-content">
          <div className="system-mode-header">
            <strong>{agentHeaderTitle}</strong>
            <span>{agentHeaderDetail}</span>
          </div>
          <div className="active-agent-stack">
            {activeAgents.map((agent) => (
              <div key={agent.id} className="active-agent-row">
                <img src={agent.iconUrl} alt="" />
                <div>
                  <span>{agent.label}</span>
                  <small>{agent.description}</small>
                </div>
              </div>
            ))}
          </div>
          <div className="system-chat-dummy">
            <strong>LLM Chat Area</strong>
            <span>Dummy chat surface</span>
          </div>
        </div>
      )}

      {mode === "notifications" && (
        <div className="system-mode-content">
          <div className="system-mode-header">
            <strong>Notifications</strong>
            <span>Alert settings</span>
          </div>
          <div className="menu-settings-list">
            {["Layout proposals", "Market alerts", "Agent status", "Risk notices"].map((item) => (
              <button key={item}>{item}</button>
            ))}
          </div>
        </div>
      )}

      {mode === "watchlist" && (
        <div className="system-mode-content">
          <div className="system-mode-header">
            <strong>Watch List</strong>
            <span>Default system area</span>
          </div>
          <div className="watchlist-dummy">
            {["AAPL", "MSFT", "NVDA", "TSLA", "SPY"].map((symbol, index) => (
              <div key={symbol} className="watchlist-row">
                <span>{symbol}</span>
                <small>{index % 2 === 0 ? "+0.8%" : "-0.3%"}</small>
              </div>
            ))}
          </div>
        </div>
      )}
    </aside>
  );
}

export function SystemOrbRail({
  agents,
  selectedAgentIds,
  settingsActive,
  notificationsActive,
  onToggleAgent,
  onToggleNotifications,
  onToggleSettings
}: {
  agents: AgentOption[];
  selectedAgentIds: string[];
  settingsActive: boolean;
  notificationsActive: boolean;
  onToggleAgent: (agentId: string) => void;
  onToggleNotifications: () => void;
  onToggleSettings: () => void;
}) {
  const agentSlots = agents.slice(0, 4);

  return (
    <div className="system-orb-rail" aria-label="System controls">
      {agentSlots.map((agent) => (
        <button
          key={agent.id}
          className={selectedAgentIds.includes(agent.id) ? "system-orb selected" : "system-orb"}
          aria-label={agent.label}
          title={agent.label}
          onClick={() => onToggleAgent(agent.id)}
        >
          <img src={agent.iconUrl} alt="" />
        </button>
      ))}
      {Array.from({ length: Math.max(0, 5 - agentSlots.length) }).map((_, index) => (
        <span key={`spacer-${index}`} className="system-orb spacer" aria-hidden="true" />
      ))}
      <button
        className={notificationsActive ? "system-orb selected" : "system-orb"}
        aria-label="Notification settings"
        title="Notification settings"
        onClick={onToggleNotifications}
      >
        <Bell size={19} />
      </button>
      <button
        className={settingsActive ? "system-orb menu selected" : "system-orb menu"}
        aria-label="Settings"
        title="Settings"
        onClick={onToggleSettings}
      >
        <Menu size={22} />
      </button>
    </div>
  );
}

function SettingsPanel({
  settingsTab,
  layout,
  agents,
  editingAgentId,
  savedLayouts,
  onSettingsTabChange,
  onEditAgent,
  onUpdateAgent,
  onAddAgent,
  onDeleteAgent,
  onCommand
}: SettingsPanelProps) {
  return (
    <div className="settings-panel">
      <div className="settings-tabs">
        <button className={settingsTab === "layouts" ? "active" : ""} onClick={() => onSettingsTabChange("layouts")}>
          Layouts
        </button>
        <button className={settingsTab === "agent" ? "active" : ""} onClick={() => onSettingsTabChange("agent")}>
          Agent
        </button>
        <button className={settingsTab === "menu" ? "active" : ""} onClick={() => onSettingsTabChange("menu")}>
          Menu
        </button>
      </div>

      {settingsTab === "layouts" && (
        <LayoutsSettings layout={layout} savedLayouts={savedLayouts} onCommand={onCommand} />
      )}

      {settingsTab === "agent" && (
        <AgentSettings
          agents={agents}
          editingAgentId={editingAgentId}
          onEditAgent={onEditAgent}
          onUpdateAgent={onUpdateAgent}
          onAddAgent={onAddAgent}
          onDeleteAgent={onDeleteAgent}
        />
      )}

      {settingsTab === "menu" && (
        <div className="menu-settings-list">
          {["Account", "Workspace", "Data Sources", "Keyboard", "Help"].map((item) => (
            <button key={item}>{item}</button>
          ))}
        </div>
      )}
    </div>
  );
}

function LayoutsSettings({
  layout,
  savedLayouts,
  onCommand
}: {
  layout: WorkspaceLayout;
  savedLayouts: SavedLayoutRecord[];
  onCommand: (command: LayoutCommand) => void;
}) {
  const defaultLayouts = savedLayouts.filter((record) => record.kind === "default");
  const userLayouts = savedLayouts.filter((record) => record.kind === "user");
  const usedSlots = new Set(savedLayouts.map((record) => record.favoriteSlot).filter(Boolean));
  const nextFavoriteSlot = findNextFavoriteSlot(usedSlots);

  return (
    <div className="layout-settings">
      <div className="settings-section-title">Default layouts</div>
      {defaultLayouts.map((record) => (
        <LayoutRecordRow
          key={record.id}
          layout={layout}
          record={record}
          nextFavoriteSlot={nextFavoriteSlot}
          onCommand={onCommand}
        />
      ))}

      <div className="settings-section-title">User layouts</div>
      {userLayouts.length === 0 ? (
        <span className="empty-layout-note">No user layouts</span>
      ) : (
        userLayouts.map((record) => (
          <LayoutRecordRow
            key={record.id}
            layout={layout}
            record={record}
            nextFavoriteSlot={nextFavoriteSlot}
            onCommand={onCommand}
          />
        ))
      )}
      <button
        className="add-layout-button"
        disabled={userLayouts.length >= MAX_USER_LAYOUTS}
        onClick={() => onCommand(makeCommand("layout.save", "user", { name: `User Layout ${userLayouts.length + 1}` }))}
      >
        <Plus size={15} /> Add layout
      </button>
    </div>
  );
}

function LayoutRecordRow({
  layout,
  record,
  nextFavoriteSlot,
  onCommand
}: {
  layout: WorkspaceLayout;
  record: SavedLayoutRecord;
  nextFavoriteSlot: FavoriteLayoutSlot | null;
  onCommand: (command: LayoutCommand) => void;
}) {
  const isSame = layoutSnapshotsEqual(layout, record.layout);
  const favoriteDisabled = !record.favoriteSlot && nextFavoriteSlot === null;

  return (
    <div className="layout-record-row">
      <button
        className="layout-record-name"
        title={record.name}
        onClick={() => onCommand(makeCommand("layout.load", "user", { savedLayoutId: record.id }))}
      >
        {record.name}
      </button>
      <button
        title={favoriteDisabled ? "Favorite slots are full" : "Favorite"}
        disabled={favoriteDisabled}
        onClick={() =>
          onCommand(
            makeCommand("layout.favorite.set", "user", {
              savedLayoutId: record.id,
              favoriteSlot: record.favoriteSlot ? null : nextFavoriteSlot
            })
          )
        }
      >
        <Star size={14} />
        {record.favoriteSlot ?? ""}
      </button>
      {record.kind === "default" && (
        <button
          title="Restore default"
          onClick={() => onCommand(makeCommand("layout.default.restore", "user", { defaultKey: record.defaultKey }))}
        >
          <RotateCcw size={14} />
        </button>
      )}
      <button
        title="Update saved state"
        disabled={isSame}
        onClick={() => onCommand(makeCommand("layout.update", "user", { savedLayoutId: record.id }))}
      >
        Edit
      </button>
      {record.kind === "user" && (
        <button
          title="Delete layout"
          onClick={() => onCommand(makeCommand("layout.delete", "user", { savedLayoutId: record.id }))}
        >
          <Trash2 size={14} />
        </button>
      )}
    </div>
  );
}

function findNextFavoriteSlot(usedSlots: Set<unknown>): FavoriteLayoutSlot | null {
  for (const slot of [1, 2, 3, 4] as const) {
    if (!usedSlots.has(slot)) {
      return slot;
    }
  }

  return null;
}

function AgentSettings({
  agents,
  editingAgentId,
  onEditAgent,
  onUpdateAgent,
  onAddAgent,
  onDeleteAgent
}: {
  agents: AgentOption[];
  editingAgentId?: string;
  onEditAgent: (agentId?: string) => void;
  onUpdateAgent: (agentId: string, patch: Partial<Pick<AgentOption, "label" | "description">>) => void;
  onAddAgent: () => void;
  onDeleteAgent: (agentId: string) => void;
}) {
  return (
    <div className="agent-settings-list">
      {agents.map((agent) => (
        <div key={agent.id} className="agent-settings-row">
          <button className="agent-settings-summary" onClick={() => onEditAgent(editingAgentId === agent.id ? undefined : agent.id)}>
            <img src={agent.iconUrl} alt="" />
            <span>{agent.label}</span>
          </button>
          {editingAgentId === agent.id && (
            <div className="agent-edit-form">
              <input
                value={agent.label}
                onChange={(event) => onUpdateAgent(agent.id, { label: event.target.value })}
                aria-label={`${agent.label} name`}
              />
              <textarea
                value={agent.description}
                onChange={(event) => onUpdateAgent(agent.id, { description: event.target.value })}
                aria-label={`${agent.label} description`}
              />
              <button onClick={() => onDeleteAgent(agent.id)} disabled={agents.length <= 1}>
                Delete
              </button>
            </div>
          )}
        </div>
      ))}
      <button className="add-layout-button" onClick={onAddAgent} disabled={agents.length >= 4}>
        <Plus size={15} /> Add agent
      </button>
    </div>
  );
}
