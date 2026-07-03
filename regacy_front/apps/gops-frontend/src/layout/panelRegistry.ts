import type {
  GridZone,
  PanelDefinition,
  PanelInstance,
  PanelPlacement,
  PanelSizeVariant,
  PanelType
} from "./types";

const layoutCommands = [
  "layout.panel.add",
  "layout.panel.remove",
  "layout.panel.move",
  "layout.boundary.resize",
  "layout.panel.replace",
  "layout.panel.pin",
  "layout.panel.unpin",
  "layout.panel.select",
  "layout.panel.priority.set",
  "layout.panels.arrange"
];

const variantDefinitions = {
  micro: { label: "미니", minArea: 1, description: "아이콘과 상태만 표시합니다." },
  compact: { label: "컴팩트", minArea: 2, description: "작은 요약 패널입니다." },
  standard: { label: "기본", minArea: 3, description: "기본 패널 UI입니다." },
  wide: { label: "와이드", minArea: 6, description: "가로형 작업 패널입니다." },
  large: { label: "대형", minArea: 9, description: "주요 작업 패널입니다." }
};

const workspaceZones: GridZone[] = ["main", "context", "mainContext"];

const workspacePlacement = (
  zone: GridZone,
  col: number,
  row: number,
  colSpan: number,
  rowSpan: number
): PanelPlacement => ({
  group: "workspace",
  zone,
  col,
  row,
  colSpan,
  rowSpan
});

export const panelRegistry: Record<PanelType, PanelDefinition> = {
  chart: {
    type: "chart",
    title: "차트",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("mainContext", 1, 1, 4, 3),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 9,
    variants: variantDefinitions,
    commands: layoutCommands
  },
  hotRanking: {
    type: "hotRanking",
    title: "Hot Ranking",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("main", 2, 4, 1, 2),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 5,
    variants: variantDefinitions,
    commands: layoutCommands
  },
  newsFeed: {
    type: "newsFeed",
    title: "시장 뉴스",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("main", 3, 4, 1, 2),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 5,
    variants: variantDefinitions,
    commands: layoutCommands
  },
  indicatorCompare: {
    type: "indicatorCompare",
    title: "지표 비교",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("main", 1, 4, 1, 2),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 6,
    variants: variantDefinitions,
    commands: layoutCommands
  },
  orderTicket: {
    type: "orderTicket",
    title: "주문",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("context", 4, 4, 1, 2),
    minSpan: { colSpan: 1, rowSpan: 2 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 7,
    variants: variantDefinitions,
    commands: layoutCommands
  },
  portfolioHoldings: {
    type: "portfolioHoldings",
    title: "내 투자",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("main", 1, 4, 1, 2),
    minSpan: { colSpan: 1, rowSpan: 2 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 7,
    variants: variantDefinitions,
    commands: layoutCommands
  },
  aiSummary: {
    type: "aiSummary",
    title: "AI 요약",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("context", 4, 4, 1, 2),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 6,
    variants: variantDefinitions,
    commands: layoutCommands,
    iconUrl: "/assets/agent-icons/agent-03.svg"
  },
  ontologyGraph: {
    type: "ontologyGraph",
    title: "온톨로지",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("context", 4, 1, 1, 2),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 6,
    variants: variantDefinitions,
    commands: layoutCommands,
    iconUrl: "/assets/agent-icons/agent-04.svg"
  },
  chartDevLog: {
    type: "chartDevLog",
    title: "Chart Dev Log",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("context", 4, 3, 1, 1),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 4,
    variants: variantDefinitions,
    commands: layoutCommands
  }
};

export const panelTypes = Object.keys(panelRegistry) as PanelType[];

export function resolvePanelVariant(panel: Pick<PanelInstance, "placement">): PanelSizeVariant {
  const area = panel.placement.colSpan * panel.placement.rowSpan;

  if (panel.placement.group === "agentRail" && area <= 1) {
    return "micro";
  }

  if (area <= 1) {
    return "compact";
  }

  if (area <= 3) {
    return "standard";
  }

  if (panel.placement.colSpan >= 3 || area <= 7) {
    return "wide";
  }

  return "large";
}

export function getPanelDefinition(type: PanelType): PanelDefinition {
  return panelRegistry[type];
}
