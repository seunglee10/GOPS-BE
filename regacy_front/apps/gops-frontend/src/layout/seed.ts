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
      : type === "newsFeed"
      ? [{ kind: "newsQuery", id: `news-${crypto.randomUUID()}` }]
      : type === "ontologyGraph"
      ? [{ kind: "ontologyGraph", id: `ontology-${crypto.randomUUID()}` }]
      : type === "orderTicket"
      ? [{ kind: "orderTicket", id: `orderTicket-${crypto.randomUUID()}` }]
      : type === "portfolioHoldings"
      ? [{ kind: "portfolioView", id: `portfolio-${crypto.randomUUID()}` }]
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

function createWorkspaceLayout(panels: PanelInstance[], selectedPanelId?: string): WorkspaceLayout {
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
          "newsFeed",
          { group: "workspace", zone: "mainContext", col: 1, row: 1, colSpan: 3, rowSpan: 3 },
          "system",
          { query: "시장 흐름" },
          "panel-news-primary"
        ),
        createPanelInstance(
          "ontologyGraph",
          { group: "workspace", zone: "context", col: 4, row: 1, colSpan: 1, rowSpan: 2 },
          "system",
          { source: "ontology" },
          "panel-ontology"
        ),
        createPanelInstance(
          "aiSummary",
          { group: "workspace", zone: "context", col: 4, row: 3, colSpan: 1, rowSpan: 1 },
          "system",
          { summary: "AI 요약 준비 중" },
          "panel-ai-summary"
        ),
        createPanelInstance(
          "chart",
          { group: "workspace", zone: "main", col: 1, row: 4, colSpan: 2, rowSpan: 2 },
          "system",
          { label: "차트 미리보기" },
          "panel-chart-preview"
        ),
        createPanelInstance(
          "indicatorCompare",
          { group: "workspace", zone: "main", col: 3, row: 4, colSpan: 1, rowSpan: 2 },
          "system",
          { label: "지표 비교" },
          "panel-indicator-compare"
        ),
        createPanelInstance(
          "orderTicket",
          { group: "workspace", zone: "context", col: 4, row: 4, colSpan: 1, rowSpan: 2 },
          "system",
          {},
          "panel-order"
        )
      ]
    );
  }

  if (key === "overview") {
    return createWorkspaceLayout(
      [
        createPanelInstance(
          "chart",
          { group: "workspace", zone: "main", col: 1, row: 1, colSpan: 2, rowSpan: 3 },
          "system",
          { label: "메인 차트" },
          "panel-chart-primary"
        ),
        createPanelInstance(
          "newsFeed",
          { group: "workspace", zone: "main", col: 3, row: 1, colSpan: 1, rowSpan: 3 },
          "system",
          { query: "시장 흐름" },
          "panel-news"
        ),
        createPanelInstance(
          "ontologyGraph",
          { group: "workspace", zone: "context", col: 4, row: 1, colSpan: 1, rowSpan: 2 },
          "system",
          { source: "ontology" },
          "panel-ontology"
        ),
        createPanelInstance(
          "aiSummary",
          { group: "workspace", zone: "context", col: 4, row: 3, colSpan: 1, rowSpan: 1 },
          "system",
          { summary: "AI 요약 준비 중" },
          "panel-ai-summary"
        ),
        createPanelInstance(
          "indicatorCompare",
          { group: "workspace", zone: "main", col: 1, row: 4, colSpan: 3, rowSpan: 2 },
          "system",
          { label: "지표 비교" },
          "panel-indicator-compare"
        ),
        createPanelInstance(
          "orderTicket",
          { group: "workspace", zone: "context", col: 4, row: 4, colSpan: 1, rowSpan: 2 },
          "system",
          {},
          "panel-order"
        )
      ]
    );
  }

  if (key === "signals") {
    return createWorkspaceLayout(
      [
        createPanelInstance(
          "indicatorCompare",
          { group: "workspace", zone: "mainContext", col: 1, row: 1, colSpan: 3, rowSpan: 3 },
          "system",
          { label: "지표 비교" },
          "panel-indicator-compare"
        ),
        createPanelInstance(
          "ontologyGraph",
          { group: "workspace", zone: "context", col: 4, row: 1, colSpan: 1, rowSpan: 2 },
          "system",
          { source: "ontology" },
          "panel-ontology"
        ),
        createPanelInstance(
          "aiSummary",
          { group: "workspace", zone: "context", col: 4, row: 3, colSpan: 1, rowSpan: 1 },
          "system",
          { summary: "시그널 요약 준비 중" },
          "panel-ai-summary"
        ),
        createPanelInstance(
          "chart",
          { group: "workspace", zone: "main", col: 1, row: 4, colSpan: 2, rowSpan: 2 },
          "system",
          { label: "시그널 차트" },
          "panel-chart-primary"
        ),
        createPanelInstance(
          "newsFeed",
          { group: "workspace", zone: "main", col: 3, row: 4, colSpan: 1, rowSpan: 2 },
          "system",
          { query: "시장 흐름" },
          "panel-news"
        ),
        createPanelInstance(
          "orderTicket",
          { group: "workspace", zone: "context", col: 4, row: 4, colSpan: 1, rowSpan: 2 },
          "system",
          {},
          "panel-order"
        )
      ]
    );
  }

  const panels: PanelInstance[] = [
    createPanelInstance(
      "chart",
      { group: "workspace", zone: "mainContext", col: 1, row: 1, colSpan: 3, rowSpan: 3 },
      "system",
      { label: "메인 차트" },
      "panel-chart-primary"
    ),
    createPanelInstance(
      "ontologyGraph",
      { group: "workspace", zone: "context", col: 4, row: 1, colSpan: 1, rowSpan: 2 },
      "system",
      { source: "ontology" },
      "panel-ontology"
    ),
    createPanelInstance(
      "chartDevLog",
      { group: "workspace", zone: "context", col: 4, row: 3, colSpan: 1, rowSpan: 1 },
      "system",
      {},
      "panel-chart-dev-log"
    ),
    createPanelInstance(
      "portfolioHoldings",
      { group: "workspace", zone: "main", col: 1, row: 4, colSpan: 1, rowSpan: 2 },
      "system",
      { source: "kis-demo" },
      "panel-portfolio"
    ),
    createPanelInstance(
      "hotRanking",
      { group: "workspace", zone: "main", col: 2, row: 4, colSpan: 1, rowSpan: 2 },
      "system",
      { source: "market-data" },
      "panel-hot-ranking"
    ),
    createPanelInstance(
      "newsFeed",
      { group: "workspace", zone: "main", col: 3, row: 4, colSpan: 1, rowSpan: 2 },
      "system",
      { query: "시장 흐름" },
      "panel-news"
    ),
    createPanelInstance(
      "orderTicket",
      { group: "workspace", zone: "context", col: 4, row: 4, colSpan: 1, rowSpan: 2 },
      "system",
      {},
      "panel-order"
    )
  ];

  return createWorkspaceLayout(panels);
}

export function createSeedLayout(): WorkspaceLayout {
  return createPresetLayout("chart");
}

export function createDefaultLayoutRecords(): SavedLayoutRecord[] {
  const now = new Date().toISOString();
  const defaults: Array<{ key: DefaultLayoutKey; name: string }> = [
    { key: "chart", name: "차트" },
    { key: "news", name: "뉴스" },
    { key: "overview", name: "개요" },
    { key: "signals", name: "시그널" }
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
