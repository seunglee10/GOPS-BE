import { useMemo, useReducer, useState } from "react";
import { MarketTicker } from "./components/MarketTicker";
import { TopAppBar } from "./components/TopAppBar";
import { initialAgentOptions, type AgentOption, type SystemMenuTab, type SystemMode } from "./components/SystemArea";
import { WorkspaceGrid } from "./components/WorkspaceGrid";
import {
  createInitialRuntimeState,
  executeCommand,
  makeCommand
} from "./layout/commands";
import type { LayoutCommand, LayoutRuntimeState } from "./layout/types";

type RuntimeAction =
  | { kind: "command"; command: LayoutCommand };

function runtimeReducer(state: LayoutRuntimeState, action: RuntimeAction): LayoutRuntimeState {
  return executeCommand(state, action.command);
}

export default function App() {
  const [state, dispatch] = useReducer(runtimeReducer, undefined, createInitialRuntimeState);
  const [activeSystemMode, setActiveSystemMode] = useState<SystemMode>("watchlist");
  const [selectedAgentIds, setSelectedAgentIds] = useState<string[]>([]);
  const [settingsTab, setSettingsTab] = useState<SystemMenuTab>("layouts");
  const [agents, setAgents] = useState<AgentOption[]>(initialAgentOptions);
  const [editingAgentId, setEditingAgentId] = useState<string | undefined>();

  const selectedPanel = useMemo(
    () => state.layout.panels.find((panel) => panel.id === state.layout.selectedPanelId),
    [state.layout.panels, state.layout.selectedPanelId]
  );

  const runCommand = (command: LayoutCommand) => dispatch({ kind: "command", command });

  const closeSystemPanel = () => {
    setSelectedAgentIds([]);
    setEditingAgentId(undefined);
    setActiveSystemMode("watchlist");
  };

  const toggleSettings = () => {
    setSelectedAgentIds([]);
    setEditingAgentId(undefined);
    setSettingsTab("layouts");
    setActiveSystemMode((current) => (current === "settings" ? "watchlist" : "settings"));
  };

  const toggleNotifications = () => {
    setSelectedAgentIds([]);
    setEditingAgentId(undefined);
    setActiveSystemMode((current) => (current === "notifications" ? "watchlist" : "notifications"));
  };

  const toggleAgent = (agentId: string) => {
    setSelectedAgentIds((current) => {
      const next = current.includes(agentId)
        ? current.filter((id) => id !== agentId)
        : [...current, agentId];

      setActiveSystemMode(next.length === 0 ? "watchlist" : "agents");
      return next;
    });
  };

  const updateAgent = (agentId: string, patch: Partial<Pick<AgentOption, "label" | "description">>) => {
    setAgents((current) => current.map((agent) => (agent.id === agentId ? { ...agent, ...patch } : agent)));
  };

  const addAgent = () => {
    setAgents((current) => {
      if (current.length >= 4) {
        return current;
      }

      const usedNumbers = new Set(
        current
          .map((agent) => Number(agent.id.replace("agent-", "")))
          .filter((value) => Number.isFinite(value))
      );
      const nextNumber = [1, 2, 3, 4].find((value) => !usedNumbers.has(value)) ?? current.length + 1;
      return [
        ...current,
        {
          id: `agent-${String(nextNumber).padStart(2, "0")}`,
          label: `Agent ${String(nextNumber).padStart(2, "0")}`,
          description: "New workspace assistant.",
          iconUrl: `/assets/agent-icons/agent-${String(nextNumber).padStart(2, "0")}.svg`
        }
      ];
    });
  };

  const deleteAgent = (agentId: string) => {
    setAgents((current) => current.filter((agent) => agent.id !== agentId));
    setSelectedAgentIds((current) => {
      const next = current.filter((id) => id !== agentId);
      setActiveSystemMode((mode) => (mode === "agents" ? (next.length === 0 ? "watchlist" : "agents") : mode));
      return next;
    });
    setEditingAgentId(undefined);
  };

  return (
    <main className="app-shell">
      <TopAppBar
        layout={state.layout}
        savedLayouts={state.savedLayouts}
        autoEnabled={state.layout.settings.llmLayoutAutoApply}
        agents={agents}
        selectedAgentIds={selectedAgentIds}
        settingsActive={activeSystemMode === "settings"}
        notificationsActive={activeSystemMode === "notifications"}
        onToggleAuto={() =>
          runCommand(
            makeCommand("layout.autoApply.set", "user", {
              value: !state.layout.settings.llmLayoutAutoApply
            })
          )
        }
        onToggleNotifications={toggleNotifications}
        onToggleAgent={toggleAgent}
        onToggleSettings={toggleSettings}
        onCommand={runCommand}
      />

      <MarketTicker />

      <section className="workspace-area" aria-label="GOPS layout workspace">
        <WorkspaceGrid
          layout={state.layout}
          selectedPanelId={selectedPanel?.id}
          systemMode={activeSystemMode}
          settingsTab={settingsTab}
          agents={agents}
          selectedAgentIds={selectedAgentIds}
          editingAgentId={editingAgentId}
          savedLayouts={state.savedLayouts}
          onSettingsTabChange={setSettingsTab}
          onEditAgent={setEditingAgentId}
          onUpdateAgent={updateAgent}
          onAddAgent={addAgent}
          onDeleteAgent={deleteAgent}
          onCloseSystemPanel={closeSystemPanel}
          onCommand={runCommand}
        />
      </section>
    </main>
  );
}

export function command(type: Parameters<typeof makeCommand>[0], payload: Record<string, unknown> = {}) {
  return makeCommand(type, "user", payload);
}
