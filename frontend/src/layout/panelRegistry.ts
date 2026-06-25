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
  "layout.panel.select"
];

const variantDefinitions = {
  micro: { label: "Micro", minArea: 1, description: "Icon and status only." },
  compact: { label: "Compact", minArea: 2, description: "Small summary surface." },
  standard: { label: "Standard", minArea: 3, description: "Default panel UI." },
  wide: { label: "Wide", minArea: 6, description: "Horizontal workspace panel." },
  large: { label: "Large", minArea: 9, description: "Primary work panel." }
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

const agentPlacement = (row: number, rowSpan = 1): PanelPlacement => ({
  group: "agentRail",
  zone: "agentRail",
  col: 1,
  row,
  colSpan: 1,
  rowSpan
});

export const panelRegistry: Record<PanelType, PanelDefinition> = {
  chart: {
    type: "chart",
    title: "Chart",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("mainContext", 1, 1, 4, 3),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 9,
    variants: variantDefinitions,
    commands: layoutCommands
  },
  watchlist: {
    type: "watchlist",
    title: "Watchlist",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("main", 1, 4, 1, 2),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 4,
    variants: variantDefinitions,
    commands: layoutCommands
  },
  newsFeed: {
    type: "newsFeed",
    title: "News Feed",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("main", 2, 4, 2, 2),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 5,
    variants: variantDefinitions,
    commands: layoutCommands
  },
  proposalReview: {
    type: "proposalReview",
    title: "Proposal Review",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("context", 4, 1, 1, 2),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 6,
    variants: variantDefinitions,
    commands: layoutCommands
  },
  agentStatus: {
    type: "agentStatus",
    title: "Agent Status",
    allowedZones: ["agentRail"],
    defaultPlacement: agentPlacement(1),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 1, rowSpan: 2 },
    defaultWeight: 3,
    variants: variantDefinitions,
    commands: layoutCommands,
    iconUrl: "/assets/agent-icons/agent-01.svg"
  },
  agentChat: {
    type: "agentChat",
    title: "Agent Chat",
    allowedZones: ["agentRail", "context"],
    defaultPlacement: agentPlacement(2, 2),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 1, rowSpan: 3 },
    defaultWeight: 5,
    variants: variantDefinitions,
    commands: layoutCommands,
    iconUrl: "/assets/agent-icons/agent-02.svg"
  },
  symbolSummary: {
    type: "symbolSummary",
    title: "Symbol Summary",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("context", 4, 3, 1, 1),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 5,
    variants: variantDefinitions,
    commands: layoutCommands
  },
  indicatorCompare: {
    type: "indicatorCompare",
    title: "Indicator Compare",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("main", 1, 4, 2, 2),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 6,
    variants: variantDefinitions,
    commands: layoutCommands
  },
  aiSummary: {
    type: "aiSummary",
    title: "AI Summary",
    allowedZones: [...workspaceZones, "agentRail"],
    defaultPlacement: workspacePlacement("context", 4, 4, 1, 2),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 6,
    variants: variantDefinitions,
    commands: layoutCommands,
    iconUrl: "/assets/agent-icons/agent-03.svg"
  },
  notifications: {
    type: "notifications",
    title: "Notifications",
    allowedZones: workspaceZones,
    defaultPlacement: workspacePlacement("context", 4, 1, 1, 1),
    minSpan: { colSpan: 1, rowSpan: 1 },
    maxSpan: { colSpan: 4, rowSpan: 5 },
    defaultWeight: 7,
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
