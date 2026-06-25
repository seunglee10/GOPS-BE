import { getPanelDefinition, resolvePanelVariant } from "./panelRegistry";
import type {
  CommandActor,
  DefaultLayoutKey,
  PanelInstance,
  PanelPlacement,
  PanelType,
  SavedLayoutRecord,
  WorkspaceLayout
} from "./types";

export const layoutZones: WorkspaceLayout["zones"] = {
  workspace: { columns: 4, rows: 5, mainColumns: 3, contextColumns: 1 },
  agentRail: { columns: 1, rows: 5 }
};

export function createPanelInstance(
  type: PanelType,
  placement: PanelPlacement,
  actor: CommandActor,
  props: Record<string, unknown> = {},
  id = `${type}-${crypto.randomUUID()}`
): PanelInstance {
  const definition = getPanelDefinition(type);
  const now = new Date().toISOString();
  const resourceRefs =
    type === "chart"
      ? [{ kind: "chartDocument", id: `chartDocument-${crypto.randomUUID()}` }]
      : type === "watchlist"
        ? [{ kind: "watchlist", id: `watchlist-${crypto.randomUUID()}` }]
        : type === "newsFeed"
          ? [{ kind: "newsQuery", id: `news-${crypto.randomUUID()}` }]
          : type === "agentChat" || type === "agentStatus"
            ? [{ kind: "agentThread", id: `agent-${crypto.randomUUID()}` }]
            : undefined;

  const chartRef = resourceRefs?.find((ref) => ref.kind === "chartDocument");
  const panel: PanelInstance = {
    id,
    type,
    title: definition.title,
    placement,
    props,
    resourceRefs,
    chartDocumentId: chartRef?.id,
    layoutPinned: false,
    layoutWeight: definition.defaultWeight,
    variant: "standard",
    createdBy: actor,
    updatedAt: now
  };

  return { ...panel, variant: resolvePanelVariant(panel) };
}

function createWorkspaceLayout(panels: PanelInstance[], selectedPanelId = panels[0]?.id): WorkspaceLayout {
  return {
    version: 1,
    zones: layoutZones,
    settings: {
      llmLayoutAutoApply: false,
      reflowMode: "auto"
    },
    panels,
    selectedPanelId
  };
}

export function createPresetLayout(key: DefaultLayoutKey): WorkspaceLayout {
  if (key === "news") {
    return createWorkspaceLayout(
      [
        createPanelInstance(
          "notifications",
          { group: "workspace", zone: "context", col: 4, row: 1, colSpan: 1, rowSpan: 1 },
          "system",
          { label: "Market and agent alerts" },
          "panel-notifications"
        ),
        createPanelInstance(
          "newsFeed",
          { group: "workspace", zone: "mainContext", col: 1, row: 1, colSpan: 3, rowSpan: 3 },
          "system",
          { query: "market pulse" },
          "panel-news-primary"
        ),
        createPanelInstance(
          "chart",
          { group: "workspace", zone: "main", col: 1, row: 4, colSpan: 2, rowSpan: 2 },
          "system",
          { label: "Chart preview" },
          "panel-chart-preview"
        ),
        createPanelInstance(
          "symbolSummary",
          { group: "workspace", zone: "main", col: 3, row: 4, colSpan: 1, rowSpan: 2 },
          "system",
          { symbol: "NVDA" },
          "panel-symbol-summary"
        ),
        createPanelInstance(
          "proposalReview",
          { group: "workspace", zone: "context", col: 4, row: 2, colSpan: 1, rowSpan: 1 },
          "system",
          { status: "No pending layout proposal" },
          "panel-proposal"
        ),
        createPanelInstance(
          "aiSummary",
          { group: "workspace", zone: "context", col: 4, row: 4, colSpan: 1, rowSpan: 2 },
          "system",
          { summary: "LLM summary placeholder" },
          "panel-ai-summary"
        )
      ],
      "panel-news-primary"
    );
  }

  if (key === "overview") {
    return createWorkspaceLayout(
      [
        createPanelInstance(
          "notifications",
          { group: "workspace", zone: "context", col: 4, row: 1, colSpan: 1, rowSpan: 1 },
          "system",
          { label: "Market and agent alerts" },
          "panel-notifications"
        ),
        createPanelInstance(
          "chart",
          { group: "workspace", zone: "main", col: 1, row: 1, colSpan: 2, rowSpan: 3 },
          "system",
          { label: "Primary chart placeholder" },
          "panel-chart-primary"
        ),
        createPanelInstance(
          "newsFeed",
          { group: "workspace", zone: "main", col: 3, row: 1, colSpan: 1, rowSpan: 3 },
          "system",
          { query: "market pulse" },
          "panel-news"
        ),
        createPanelInstance(
          "watchlist",
          { group: "workspace", zone: "main", col: 1, row: 4, colSpan: 1, rowSpan: 2 },
          "system",
          { symbols: ["AAPL", "MSFT", "NVDA"] },
          "panel-watchlist"
        ),
        createPanelInstance(
          "indicatorCompare",
          { group: "workspace", zone: "main", col: 2, row: 4, colSpan: 2, rowSpan: 2 },
          "system",
          { label: "Indicator comparison" },
          "panel-indicator-compare"
        ),
        createPanelInstance(
          "proposalReview",
          { group: "workspace", zone: "context", col: 4, row: 2, colSpan: 1, rowSpan: 1 },
          "system",
          { status: "No pending layout proposal" },
          "panel-proposal"
        ),
        createPanelInstance(
          "aiSummary",
          { group: "workspace", zone: "context", col: 4, row: 4, colSpan: 1, rowSpan: 2 },
          "system",
          { summary: "LLM summary placeholder" },
          "panel-ai-summary"
        )
      ],
      "panel-chart-primary"
    );
  }

  if (key === "signals") {
    return createWorkspaceLayout(
      [
        createPanelInstance(
          "notifications",
          { group: "workspace", zone: "context", col: 4, row: 1, colSpan: 1, rowSpan: 1 },
          "system",
          { label: "Signal and market alerts" },
          "panel-notifications"
        ),
        createPanelInstance(
          "chart",
          { group: "workspace", zone: "main", col: 1, row: 1, colSpan: 2, rowSpan: 3 },
          "system",
          { label: "Signal chart placeholder" },
          "panel-chart-primary"
        ),
        createPanelInstance(
          "indicatorCompare",
          { group: "workspace", zone: "main", col: 3, row: 1, colSpan: 1, rowSpan: 3 },
          "system",
          { label: "Indicator comparison" },
          "panel-indicator-compare"
        ),
        createPanelInstance(
          "symbolSummary",
          { group: "workspace", zone: "main", col: 1, row: 4, colSpan: 1, rowSpan: 2 },
          "system",
          { symbol: "NVDA" },
          "panel-symbol-summary"
        ),
        createPanelInstance(
          "aiSummary",
          { group: "workspace", zone: "main", col: 2, row: 4, colSpan: 2, rowSpan: 2 },
          "system",
          { summary: "Signal summary placeholder" },
          "panel-ai-summary"
        ),
        createPanelInstance(
          "proposalReview",
          { group: "workspace", zone: "context", col: 4, row: 2, colSpan: 1, rowSpan: 2 },
          "system",
          { status: "No pending signal proposal" },
          "panel-proposal"
        ),
        createPanelInstance(
          "watchlist",
          { group: "workspace", zone: "context", col: 4, row: 4, colSpan: 1, rowSpan: 2 },
          "system",
          { symbols: ["AAPL", "MSFT", "NVDA"] },
          "panel-watchlist"
        )
      ],
      "panel-chart-primary"
    );
  }

  const panels: PanelInstance[] = [
    createPanelInstance(
      "notifications",
      { group: "workspace", zone: "context", col: 4, row: 1, colSpan: 1, rowSpan: 1 },
      "system",
      { label: "Market and agent alerts" },
      "panel-notifications"
    ),
    createPanelInstance(
      "chart",
      { group: "workspace", zone: "mainContext", col: 1, row: 1, colSpan: 3, rowSpan: 3 },
      "system",
      { label: "Primary chart placeholder" },
      "panel-chart-primary"
    ),
    createPanelInstance(
      "proposalReview",
      { group: "workspace", zone: "context", col: 4, row: 2, colSpan: 1, rowSpan: 1 },
      "system",
      { status: "No pending chart proposal" },
      "panel-proposal"
    ),
    createPanelInstance(
      "watchlist",
      { group: "workspace", zone: "main", col: 1, row: 4, colSpan: 1, rowSpan: 2 },
      "system",
      { symbols: ["AAPL", "MSFT", "NVDA"] },
      "panel-watchlist"
    ),
    createPanelInstance(
      "newsFeed",
      { group: "workspace", zone: "main", col: 2, row: 4, colSpan: 2, rowSpan: 2 },
      "system",
      { query: "market pulse" },
      "panel-news"
    ),
    createPanelInstance(
      "symbolSummary",
      { group: "workspace", zone: "context", col: 4, row: 3, colSpan: 1, rowSpan: 1 },
      "system",
      { symbol: "NVDA" },
      "panel-symbol-summary"
    ),
    createPanelInstance(
      "aiSummary",
      { group: "workspace", zone: "context", col: 4, row: 4, colSpan: 1, rowSpan: 2 },
      "system",
      { summary: "LLM summary placeholder" },
      "panel-ai-summary"
    )
  ];

  return createWorkspaceLayout(panels, "panel-chart-primary");
}

export function createSeedLayout(): WorkspaceLayout {
  return createPresetLayout("chart");
}

export function createDefaultLayoutRecords(): SavedLayoutRecord[] {
  const now = new Date().toISOString();
  const defaults: Array<{ key: DefaultLayoutKey; name: string }> = [
    { key: "chart", name: "Chart" },
    { key: "news", name: "News" },
    { key: "overview", name: "Overview" },
    { key: "signals", name: "Signals" }
  ];

  return defaults.map(({ key, name }) => ({
    id: `default-${key}`,
    name,
    version: 1,
    savedAt: now,
    kind: "default",
    defaultKey: key,
    layout: createPresetLayout(key)
  }));
}
