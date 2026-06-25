import type {
  ChartId,
  ChartToolMode,
  ChartViewport,
  ComparisonSeriesLayer,
  DocumentId,
  DrawingLayer,
  IndicatorLayer,
  LayerId,
  LayerStyle,
  PaneDocument,
  PaneId,
  PanelId,
  PinMode,
  WorkspaceDocument
} from "./documents";
import type { CalculationNode } from "./calculations";
import type { SymbolCode, Timeframe } from "./market";

export type CommandActor = "user" | "ai" | "system";
export type CommandStatus = "proposal" | "accepted" | "rejected" | "applied" | "blocked" | "failed";

export interface BaseCommand<TType extends string, TPayload> {
  id: string;
  type: TType;
  actor: CommandActor;
  status: CommandStatus;
  target: CommandTarget;
  payload: TPayload;
  reason?: string;
  proposalId?: string;
  createdAt: string;
}

export interface CommandTarget {
  workspaceId: DocumentId;
  panelId: PanelId;
  chartId: ChartId;
  paneId?: PaneId;
  layerId?: LayerId;
}

export type SetChartSymbolCommand = BaseCommand<"chart.symbol.set", { symbol: SymbolCode }>;
export type SetChartTimeframeCommand = BaseCommand<"chart.timeframe.set", { timeframe: Timeframe }>;
export type SetChartViewportCommand = BaseCommand<"chart.viewport.set", Partial<ChartViewport>>;

export type AddIndicatorCommand = BaseCommand<
  "chart.indicator.add",
  {
    node: CalculationNode;
    layer: IndicatorLayer;
    pane?: PaneDocument;
  }
>;

export type UpdateIndicatorCommand = BaseCommand<
  "chart.indicator.update",
  {
    calculationNodeId: string;
    inputs: Record<string, string | number | boolean | string[]>;
    layerPatch?: Partial<IndicatorLayer>;
  }
>;

export type RemoveIndicatorCommand = BaseCommand<"chart.indicator.remove", { calculationNodeId: string; layerId: LayerId }>;
export type AddDrawingCommand = BaseCommand<"chart.drawing.add", { layer: DrawingLayer }>;
export type UpdateDrawingCommand = BaseCommand<
  "chart.drawing.update",
  { layerId: LayerId; drawing: DrawingLayer["drawing"]; style?: LayerStyle; visible?: boolean }
>;
export type RemoveDrawingCommand = BaseCommand<"chart.drawing.remove", { layerId: LayerId }>;
export type AddComparisonCommand = BaseCommand<"chart.comparison.add", { layer: ComparisonSeriesLayer }>;
export type RemoveComparisonCommand = BaseCommand<"chart.comparison.remove", { layerId: LayerId }>;
export type SetLayerVisibilityCommand = BaseCommand<"chart.layer.visibility.set", { layerId: LayerId; visible: boolean }>;
export type SetPanelPinModeCommand = BaseCommand<"panel.pinMode.set", { panelId: PanelId; pinMode: PinMode }>;
export type SetPanelChartToolCommand = BaseCommand<"panel.chartTool.set", { panelId: PanelId; toolMode: ChartToolMode }>;
export type SetPanelCrosshairCommand = BaseCommand<"panel.crosshair.set", { panelId: PanelId; showCrosshair: boolean }>;
export type AcceptProposalCommand = BaseCommand<"proposal.accept", { proposalId: string }>;
export type RejectProposalCommand = BaseCommand<"proposal.reject", { proposalId: string; reason?: string }>;

export type Command =
  | SetChartSymbolCommand
  | SetChartTimeframeCommand
  | SetChartViewportCommand
  | AddIndicatorCommand
  | UpdateIndicatorCommand
  | RemoveIndicatorCommand
  | AddDrawingCommand
  | UpdateDrawingCommand
  | RemoveDrawingCommand
  | AddComparisonCommand
  | RemoveComparisonCommand
  | SetLayerVisibilityCommand
  | SetPanelPinModeCommand
  | SetPanelChartToolCommand
  | SetPanelCrosshairCommand
  | AcceptProposalCommand
  | RejectProposalCommand;

export type CommandType = Command["type"];

export interface ChartProposalDocument {
  id: string;
  source: "llm";
  status: "pending" | "accepted" | "rejected" | "invalid";
  targetPanelId: PanelId;
  targetChartId: ChartId;
  title: string;
  rationale: string;
  previewSummary: string;
  commands: Command[];
  createdAt: string;
  validationErrors: CommandValidationError[];
}

export interface CommandApplyResult {
  ok: boolean;
  status: CommandStatus;
  commandId: string;
  document: WorkspaceDocument;
  diff?: DocumentDiff;
  errors: CommandValidationError[];
}

export interface CommandValidationError {
  code:
    | "unknown_command_type"
    | "invalid_payload"
    | "missing_target"
    | "target_not_found"
    | "panel_locked"
    | "approval_required"
    | "document_limit_exceeded"
    | "layer_type_not_allowed"
    | "calculation_node_not_found"
    | "unsafe_ai_command";
  message: string;
  path?: string;
}

export interface DocumentDiff {
  beforeVersion: number;
  afterVersion: number;
  changedPaths: string[];
}

export interface CommandJournalEntry {
  id: string;
  commandId: string;
  actor: CommandActor;
  status: CommandStatus;
  changedPaths: string[];
  errors: CommandValidationError[];
  createdAt: string;
  groupId?: string;
  proposalId?: string;
}

export interface CommandDefinition<TCommand extends Command = Command> {
  type: TCommand["type"];
  validate(command: TCommand, document: WorkspaceDocument): CommandValidationError[];
  apply(command: TCommand, document: WorkspaceDocument): WorkspaceDocument;
}

export type CommandRegistry = Partial<Record<Command["type"], CommandDefinition>>;
