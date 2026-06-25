import type { ChartViewport, LayerId, LayerType, Owner, PaneDocument, PaneId, PanelId, PanelType, PinMode } from "./documents";
import type { MarketSummary } from "./calculations";
import type { ChartProposalDocument, Command, CommandType } from "./commands";
import type { SymbolCode, Timeframe } from "./market";

export interface WorkspaceContextForLlm {
  activePanelId: PanelId;
  activeChartId: string;
  panels: Array<{
    id: PanelId;
    type: PanelType;
    title: string;
    pinMode: PinMode;
    targetChartId?: string;
  }>;
  pendingProposalCount: number;
}

export interface ChartContextForLlm {
  id: string;
  symbol: SymbolCode;
  timeframe: Timeframe;
  viewport: ChartViewport;
  panes: Array<{
    id: PaneId;
    kind: PaneDocument["kind"];
    title: string;
  }>;
  layers: Array<{
    id: LayerId;
    type: LayerType;
    paneId: PaneId;
    owner: Owner;
    visible: boolean;
    summary: string;
  }>;
  availableCommands: CommandType[];
}

export interface ChatRequest {
  message: string;
  workspace: WorkspaceContextForLlm;
  chart: ChartContextForLlm;
  market: MarketSummary;
}

export interface LlmInsight {
  title: string;
  description: string;
  severity: "info" | "watch" | "important";
  relatedSymbol?: SymbolCode | null;
}

export interface LlmChartProposal {
  title: string;
  rationale: string;
  previewSummary: string;
  commands: LlmCommand[];
}

export type LlmCommand = Omit<Command, "id" | "actor" | "status" | "createdAt" | "proposalId">;

export interface ChatResponse {
  id: string;
  message: string;
  insights: LlmInsight[];
  chartProposals: ChartProposalDocument[];
  usage?: {
    inputTokens?: number;
    outputTokens?: number;
  };
  model: string;
  createdAt: string;
}

export interface ChatErrorResponse {
  error: {
    code:
      | "openai_api_key_missing"
      | "openai_timeout"
      | "openai_request_failed"
      | "llm_response_invalid"
      | "internal_error";
    message: string;
  };
}
