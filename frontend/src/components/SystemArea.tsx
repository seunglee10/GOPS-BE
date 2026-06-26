import { Bell, LoaderCircle, Menu, Plus, RotateCcw, SendHorizontal, Star, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useState, type PointerEvent as ReactPointerEvent } from "react";
import { getChartAgentAccess } from "../chart/agentAccess";
import { createChatMessage, normalizeAgentChatResponse, type AgentChatMessage } from "../chart/agentChat";
import { buildChartAgentContext } from "../chart/proposals";
import type { SupportedSymbol, WatchlistSymbol } from "../chart/symbols";
import {
  getCandlesForDocument,
  getChartDocumentForPanel,
  getStreamStatusForDocument,
  type ChartRuntimeAction,
  type ChartRuntimeState
} from "../chart/runtime";
import { MAX_USER_LAYOUTS, layoutSnapshotsEqual, makeCommand } from "../layout/commands";
import { getPanelDefinition } from "../layout/panelRegistry";
import {
  createPanelDropCommand,
  getWorkspaceDropCell,
  PANEL_CATALOG_MIME,
  PANEL_CATALOG_TYPES
} from "../layout/panelCatalogDrop";
import type { FavoriteLayoutSlot, LayoutCommand, PanelType, SavedLayoutRecord, WorkspaceLayout } from "../layout/types";

export type SystemMode = "watchlist" | "settings" | "agents" | "notifications";

export type SystemMenuTab = "layouts" | "panels" | "agent" | "menu";

export type AgentOption = {
  id: string;
  label: string;
  description: string;
  iconUrl: string;
};

export const initialAgentOptions: AgentOption[] = [
  { id: "agent-01", label: "Chart Agent", description: "LLM chart operator. It explains intent and sends chart commands.", iconUrl: "/assets/agent-icons/agent-01.svg" },
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
  chartRuntime: ChartRuntimeState;
  chartAutoApplyEnabled: boolean;
  agents: AgentOption[];
  selectedAgentIds: string[];
  editingAgentId?: string;
  savedLayouts: SavedLayoutRecord[];
  activeSymbol: SupportedSymbol;
  watchlistSymbols: WatchlistSymbol[];
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

type SettingsPanelProps = {
  settingsTab: SystemMenuTab;
  layout: WorkspaceLayout;
  activeSymbol: SupportedSymbol;
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
  chartRuntime,
  chartAutoApplyEnabled,
  agents,
  selectedAgentIds,
  editingAgentId,
  savedLayouts,
  activeSymbol,
  watchlistSymbols,
  onSettingsTabChange,
  onEditAgent,
  onUpdateAgent,
  onAddAgent,
  onDeleteAgent,
  onCloseSystemPanel,
  onSelectSymbol,
  onCommand,
  onChartAction
}: SystemAreaProps) {
  const selectedAgents = agents.filter((agent) => selectedAgentIds.includes(agent.id));
  const activeAgents = selectedAgents.length > 1 ? [orchestratorAgent, ...selectedAgents] : selectedAgents;
  const chartAgentAccess = getChartAgentAccess(selectedAgents);
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
          activeSymbol={activeSymbol}
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
        <div className="system-mode-content agent-mode-content">
          <div className="system-mode-header agent-header">
            <strong>{agentHeaderTitle}</strong>
            <span>{agentHeaderDetail}</span>
          </div>
          <AgentChatPanel
            layout={layout}
            chartRuntime={chartRuntime}
            autoApplyEnabled={chartAutoApplyEnabled}
            selectedAgents={selectedAgents}
            activeAgents={activeAgents}
            chartAgentAccess={chartAgentAccess}
            onChartAction={onChartAction}
          />
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
            <span>{activeSymbol} selected</span>
          </div>
          <div className="watchlist-list">
            {watchlistSymbols.map((item) => (
              <button
                key={item.symbol}
                className={item.symbol === activeSymbol ? "watchlist-row active" : "watchlist-row"}
                title={`Load ${item.symbol}`}
                onClick={() => onSelectSymbol(item.symbol)}
              >
                <span>
                  <strong>{item.symbol}</strong>
                  <em>{item.name}</em>
                </span>
                <small className={typeof item.changePercent === "number" && item.changePercent < 0 ? "market-down" : "market-up"}>
                  {typeof item.lastPrice === "number" ? item.lastPrice.toFixed(2) : "--"}
                  {typeof item.changePercent === "number" ? ` ${item.changePercent >= 0 ? "+" : ""}${item.changePercent.toFixed(2)}%` : ""}
                </small>
              </button>
            ))}
          </div>
        </div>
      )}
    </aside>
  );
}

function AgentChatPanel({
  layout,
  chartRuntime,
  autoApplyEnabled,
  selectedAgents,
  activeAgents,
  chartAgentAccess,
  onChartAction
}: {
  layout: WorkspaceLayout;
  chartRuntime: ChartRuntimeState;
  autoApplyEnabled: boolean;
  selectedAgents: AgentOption[];
  activeAgents: AgentOption[];
  chartAgentAccess: ReturnType<typeof getChartAgentAccess>;
  onChartAction: (action: ChartRuntimeAction) => void;
}) {
  const chartPanel = useMemo(
    () => layout.panels.find((panel) => panel.type === "chart" && panel.id === layout.selectedPanelId) ??
      layout.panels.find((panel) => panel.type === "chart"),
    [layout.panels, layout.selectedPanelId]
  );
  const chartDocument = chartPanel ? getChartDocumentForPanel(chartRuntime, chartPanel) : null;
  const candles = chartDocument ? getCandlesForDocument(chartRuntime, chartDocument) : [];
  const streamStatus = chartDocument ? getStreamStatusForDocument(chartRuntime, chartDocument) : "stale";
  const [messages, setMessages] = useState<AgentChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [agentError, setAgentError] = useState(false);
  const selectedAgentKey = selectedAgents.map((agent) => agent.id).join("|");
  const introAgent = activeAgents[0] ?? selectedAgents[0] ?? orchestratorAgent;
  const introDescription = selectedAgents.length > 1
    ? selectedAgents.map((agent) => agent.label).join(" / ")
    : introAgent.description;
  const target = chartAgentAccess.enabled && chartPanel && chartDocument ? { panelId: chartPanel.id, chartDocumentId: chartDocument.id } : null;
  const signalState = sending ? "thinking" : agentError ? "error" : "waiting";
  const signalLabel = signalState === "thinking" ? "생각 중" : signalState === "error" ? "오류" : "대기 중";
  const disabledMessage = chartAgentAccess.reason === "orchestration"
    ? "멀티에이전트 모드에서는 차트 채팅을 비활성화합니다. 차트 수정은 Agent 01 단독 선택에서만 가능합니다."
    : "이 에이전트는 현재 차트 수정 권한이 없습니다. 소개만 표시됩니다.";
  const sendDisabled = !target || !draft.trim() || sending;

  useEffect(() => {
    setMessages([]);
    setDraft("");
    setSending(false);
    setAgentError(false);
  }, [selectedAgentKey]);

  const sendMessage = () => {
    const content = draft.trim();
    if (!content || !target || !chartPanel || !chartDocument || sending) {
      return;
    }

    const userMessage = createChatMessage("user", content);
    const requestMessages = [...messages, userMessage];
    setMessages(requestMessages);
    setDraft("");
    setSending(true);
    setAgentError(false);

    fetch("/api/llm/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        agentIds: selectedAgents.map((agent) => agent.id),
        messages: requestMessages.map((message) => ({ role: message.role, content: message.content })),
        context: buildChartAgentContext({
          panelId: chartPanel.id,
          document: chartDocument,
          candles,
          streamStatus
        })
      })
    })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`Agent chat API returned ${response.status}`);
        }
        return response.json() as Promise<unknown>;
      })
      .then((payload) => {
        const result = normalizeAgentChatResponse(payload, target);
        const nextMessages = [createChatMessage("assistant", result.reply)];
        if (result.proposal) {
          onChartAction({ kind: "chart.proposal.received", proposal: result.proposal, autoApply: autoApplyEnabled });
          nextMessages.push(createChatMessage(
            "system",
            autoApplyEnabled
              ? "Chart commands were applied through the chart runtime."
              : "Chart proposal is waiting for review in the chart panel."
          ));
        }
        setMessages((current) => [...current, ...nextMessages]);
      })
      .catch((error: unknown) => {
        setAgentError(true);
        setMessages((current) => [
          ...current,
          createChatMessage("assistant", error instanceof Error ? error.message : "Agent chat failed.")
        ]);
      })
      .finally(() => setSending(false));
  };

  return (
    <div className="agent-chat-panel">
      <div className={messages.length === 0 ? "agent-chat-messages empty" : "agent-chat-messages"} aria-label="LLM chart chat messages">
        {messages.length === 0 && (
          <div className="agent-chat-empty-state">
            <img src={introAgent.iconUrl} alt="" />
            <strong>{introAgent.label}</strong>
            <span>{introDescription}</span>
            {!target && <small>{disabledMessage}</small>}
          </div>
        )}
        {target && messages.map((message) => (
          <div key={message.id} className={`agent-chat-message ${message.role}`}>
            {message.content}
          </div>
        ))}
      </div>
      <div className="agent-chat-composer">
        <div className="agent-chat-reference">
          <div className="agent-chat-reference-list" aria-label="Agent references">
            <span className="agent-chat-reference-token">{chartDocument ? chartDocument.symbol : "No chart"}</span>
          </div>
          <span className={`agent-chat-signal ${signalState}`} title={signalLabel} aria-label={signalLabel} />
        </div>
        <div className="agent-chat-input-row">
          <textarea
            value={draft}
            placeholder={target ? "Ask Agent 01 to change the chart" : disabledMessage}
            disabled={!target || sending}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                sendMessage();
              }
            }}
          />
          <button title={target ? "Send chart request" : disabledMessage} disabled={sendDisabled} onClick={sendMessage}>
            {sending ? <LoaderCircle size={15} /> : <SendHorizontal size={15} />}
          </button>
        </div>
      </div>
    </div>
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
  activeSymbol,
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
        <button className={settingsTab === "panels" ? "active" : ""} onClick={() => onSettingsTabChange("panels")}>
          Panels
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

      {settingsTab === "panels" && <PanelsCatalog layout={layout} activeSymbol={activeSymbol} onCommand={onCommand} />}

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

function PanelsCatalog({
  layout,
  activeSymbol,
  onCommand
}: {
  layout: WorkspaceLayout;
  activeSymbol: SupportedSymbol;
  onCommand: (command: LayoutCommand) => void;
}) {
  return (
    <div className="panel-catalog-list" aria-label="Panel catalog">
      <div className="settings-section-title">Workspace panels</div>
      {PANEL_CATALOG_TYPES.map((panelType) => (
        <PanelCatalogItem
          key={panelType}
          panelType={panelType}
          layout={layout}
          activeSymbol={activeSymbol}
          onCommand={onCommand}
        />
      ))}
    </div>
  );
}

function PanelCatalogItem({
  panelType,
  layout,
  activeSymbol,
  onCommand
}: {
  panelType: PanelType;
  layout: WorkspaceLayout;
  activeSymbol: SupportedSymbol;
  onCommand: (command: LayoutCommand) => void;
}) {
  const definition = getPanelDefinition(panelType);
  const beginPointerDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) {
      return;
    }

    const source = event.currentTarget;
    const startX = event.clientX;
    const startY = event.clientY;
    let latestX = startX;
    let latestY = startY;
    let moved = false;

    source.setPointerCapture?.(event.pointerId);

    const handleMove = (moveEvent: PointerEvent) => {
      latestX = moveEvent.clientX;
      latestY = moveEvent.clientY;
      moved = moved || Math.abs(latestX - startX) + Math.abs(latestY - startY) > 8;
    };

    const handleUp = (upEvent: PointerEvent) => {
      source.removeEventListener("pointermove", handleMove);
      source.removeEventListener("pointerup", handleUp);
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", handleUp);
      try {
        source.releasePointerCapture?.(event.pointerId);
      } catch {
        // Pointer capture may already be released when native drag is active.
      }

      if (!moved) {
        return;
      }

      const dropTarget = document.elementFromPoint(upEvent.clientX, upEvent.clientY);
      const frame = dropTarget?.closest(".layout-frame");
      if (!(frame instanceof HTMLElement)) {
        return;
      }

      const targetPanelId = dropTarget?.closest<HTMLElement>("[data-panel-id]")?.dataset.panelId ?? null;
      const cell = getWorkspaceDropCell(frame.getBoundingClientRect(), upEvent.clientX, upEvent.clientY);
      const command = createPanelDropCommand({ layout, panelType, activeSymbol, cell, targetPanelId });
      if (command) {
        onCommand(command);
      }
    };

    source.addEventListener("pointermove", handleMove);
    source.addEventListener("pointerup", handleUp, { once: true });
    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", handleUp, { once: true });
  };

  return (
    <div
      className="panel-catalog-item"
      draggable
      role="button"
      tabIndex={0}
      title={`Drag ${definition.title} into the workspace`}
      onDragStart={(event) => {
        event.dataTransfer.setData(PANEL_CATALOG_MIME, panelType);
        event.dataTransfer.setData("text/plain", panelType);
        event.dataTransfer.effectAllowed = "copy";
      }}
      onPointerDown={beginPointerDrag}
    >
      <strong>{definition.title}</strong>
      <span>{catalogDescription(panelType)}</span>
    </div>
  );
}

function catalogDescription(panelType: PanelType): string {
  switch (panelType) {
    case "chart":
      return "Active ticker chart document.";
    case "newsFeed":
      return "Market headlines and context.";
    case "symbolSummary":
      return "Focused symbol snapshot.";
    case "aiSummary":
      return "LLM analysis surface.";
    case "watchlist":
      return "Ticker list panel.";
    case "indicatorCompare":
      return "Indicator comparison panel.";
    case "proposalReview":
      return "Pending proposal review.";
    case "notifications":
      return "Alert and event feed.";
    default:
      return "Workspace panel.";
  }
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
