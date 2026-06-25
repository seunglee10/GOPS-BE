export type GridGroup = "workspace" | "agentRail";

export type GridZone = "main" | "context" | "mainContext" | "agentRail";

export type PanelSizeVariant = "micro" | "compact" | "standard" | "wide" | "large";

export type CommandActor = "user" | "llm" | "system";

export type FavoriteLayoutSlot = 1 | 2 | 3 | 4;

export type SavedLayoutKind = "default" | "user";

export type DefaultLayoutKey = "chart" | "news" | "overview" | "signals";

export type PanelType =
  | "chart"
  | "watchlist"
  | "newsFeed"
  | "proposalReview"
  | "agentStatus"
  | "agentChat"
  | "symbolSummary"
  | "indicatorCompare"
  | "aiSummary"
  | "notifications";

export type PanelPlacement = {
  group: GridGroup;
  zone: GridZone;
  col: number;
  row: number;
  colSpan: number;
  rowSpan: number;
  zIndex?: number;
};

export type PanelResourceRef = {
  kind: "chartDocument" | "newsQuery" | "agentThread" | "portfolioView" | "watchlist" | string;
  id: string;
};

export type PanelInstance = {
  id: string;
  type: PanelType;
  title?: string;
  placement: PanelPlacement;
  props: Record<string, unknown>;
  resourceRefs?: PanelResourceRef[];
  chartDocumentId?: string;
  layoutPinned?: boolean;
  layoutWeight?: number;
  variant: PanelSizeVariant;
  createdBy: CommandActor;
  updatedAt: string;
};

export type LayoutPreviewItem = {
  panelId: string;
  placement: PanelPlacement;
};

export type PanelVariantDefinition = {
  label: string;
  minArea: number;
  description: string;
};

export type PanelDefinition = {
  type: PanelType;
  title: string;
  allowedZones: GridZone[];
  defaultPlacement: PanelPlacement;
  minSpan: { colSpan: number; rowSpan: number };
  maxSpan?: { colSpan: number; rowSpan: number };
  defaultWeight: number;
  variants: Partial<Record<PanelSizeVariant, PanelVariantDefinition>>;
  commands: string[];
  iconUrl?: string;
};

export type WorkspaceLayoutSettings = {
  llmLayoutAutoApply: boolean;
  reflowMode: "auto";
};

export type WorkspaceLayout = {
  version: 1;
  zones: {
    workspace: { columns: 4; rows: 5; mainColumns: 3; contextColumns: 1 };
    agentRail: { columns: 1; rows: 5 };
  };
  settings: WorkspaceLayoutSettings;
  panels: PanelInstance[];
  selectedPanelId?: string;
};

export type LayoutCommandType =
  | "layout.panel.add"
  | "layout.panel.remove"
  | "layout.panel.move"
  | "layout.panel.replace"
  | "layout.panel.pin"
  | "layout.panel.unpin"
  | "layout.panel.select"
  | "layout.boundary.resize"
  | "layout.reflow"
  | "layout.undo"
  | "layout.redo"
  | "layout.save"
  | "layout.update"
  | "layout.delete"
  | "layout.load"
  | "layout.favorite.set"
  | "layout.default.restore"
  | "layout.reset"
  | "layout.autoApply.set"
  | "layout.proposal.accept"
  | "layout.proposal.reject";

export type LayoutCommand = {
  id: string;
  type: LayoutCommandType;
  actor: CommandActor;
  target?: { panelId?: string; group?: GridGroup; zone?: GridZone };
  payload: Record<string, unknown>;
  createdAt: string;
  proposalId?: string;
};

export type CommandJournalEntry = {
  id: string;
  commandType: LayoutCommandType;
  actor: CommandActor;
  status: "applied" | "failed" | "proposed" | "undone" | "redone";
  message: string;
  createdAt: string;
};

export type SavedLayoutRecord = {
  id: string;
  name: string;
  version: 1;
  savedAt: string;
  kind: SavedLayoutKind;
  defaultKey?: DefaultLayoutKey;
  layout: WorkspaceLayout;
  favoriteSlot?: FavoriteLayoutSlot;
};

export type LayoutProposal = {
  id: string;
  title: string;
  rationale: string;
  commands: LayoutCommand[];
  createdAt: string;
};

export type RuntimeError = {
  id: string;
  message: string;
  createdAt: string;
};

export type LayoutRuntimeState = {
  layout: WorkspaceLayout;
  history: WorkspaceLayout[];
  future: WorkspaceLayout[];
  journal: CommandJournalEntry[];
  errors: RuntimeError[];
  pendingProposals: LayoutProposal[];
  savedLayouts: SavedLayoutRecord[];
};
