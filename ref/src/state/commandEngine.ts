import { defaultCommandRegistry } from "./commandRegistry";
import { isCommandTypeEnabled } from "../capabilities/chartCapabilities";
import type {
  Command,
  CommandApplyResult,
  CommandDefinition,
  ChartProposalDocument,
  CommandJournalEntry,
  CommandRegistry,
  CommandValidationError
} from "../types/commands";
import type { PanelDocument, WorkspaceDocument } from "../types/documents";

export function createCommandId(prefix = "cmd"): string {
  return `${prefix}-${globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2)}`;
}

export function nowIso(): string {
  return new Date().toISOString();
}

export function validateCommand(
  document: WorkspaceDocument,
  commandLike: unknown,
  registry: CommandRegistry = defaultCommandRegistry
): CommandValidationError[] {
  const command = commandLike as Partial<Command>;
  const errors: CommandValidationError[] = [];
  if (!command || typeof command !== "object") {
    return [error("invalid_payload", "Command must be an object.")];
  }
  if (!command.type || !registry[command.type as Command["type"]] || !isCommandTypeEnabled(command.type)) {
    return [error("unknown_command_type", `Unknown command type: ${String(command.type)}.`, "type")];
  }
  if (!command.target) {
    errors.push(error("missing_target", "Command target is required.", "target"));
    return errors;
  }
  if (command.target.workspaceId !== document.id) {
    errors.push(error("target_not_found", "Target workspace was not found.", "target.workspaceId"));
  }
  const panel = document.panels.find((item) => item.id === command.target?.panelId);
  if (!panel) {
    errors.push(error("target_not_found", "Target panel was not found.", "target.panelId"));
  }
  const chart = document.charts.find((item) => item.id === command.target?.chartId);
  if (!chart) {
    errors.push(error("target_not_found", "Target chart was not found.", "target.chartId"));
  }
  if (panel && command.actor === "ai") {
    if (panel.pinMode === "locked") {
      errors.push(error("panel_locked", "AI commands cannot mutate locked panels.", "target.panelId"));
    } else if (panel.pinMode === "approval") {
      errors.push(error("approval_required", "AI commands must be accepted by the user before application.", "actor"));
    }
    if (!command.proposalId) {
      errors.push(error("unsafe_ai_command", "AI commands must originate from a validated proposal.", "proposalId"));
    } else if (!document.proposals.some((proposal) => proposal.id === command.proposalId)) {
      errors.push(error("target_not_found", "AI command proposal was not found.", "proposalId"));
    }
  }
  if (chart && command.target.paneId && !chart.panes.some((pane) => pane.id === command.target?.paneId)) {
    errors.push(error("target_not_found", "Target pane was not found.", "target.paneId"));
  }
  if (errors.length > 0) return errors;
  return (registry[command.type as Command["type"]] as CommandDefinition).validate(command as Command, document);
}

export function applyCommand(
  document: WorkspaceDocument,
  command: Command,
  registry: CommandRegistry = defaultCommandRegistry
): CommandApplyResult {
  if (command.type === "proposal.accept") {
    return applyAcceptProposal(document, command, registry);
  }
  if (command.type === "proposal.reject") {
    return applyRejectProposal(document, command, registry);
  }
  return applySingleCommand(document, command, registry);
}

export function ingestIncomingProposals(document: WorkspaceDocument, proposals: ChartProposalDocument[]): WorkspaceDocument {
  const existingIds = new Set(document.proposals.map((proposal) => proposal.id));
  const incoming = proposals.filter((proposal) => !existingIds.has(proposal.id));
  if (incoming.length === 0) return document;
  const createdAt = nowIso();
  const journalEntry: CommandJournalEntry = {
    id: createCommandId("journal"),
    commandId: createCommandId("proposal-ingest"),
    actor: "system",
    status: "applied",
    changedPaths: incoming.map((proposal) => `/proposals/${proposal.id}`),
    errors: [],
    createdAt
  };
  return {
    ...document,
    version: document.version + 1,
    proposals: [...document.proposals, ...incoming],
    commandJournal: [...document.commandJournal, journalEntry],
    updatedAt: createdAt
  };
}

export interface ProposalIngestResult {
  document: WorkspaceDocument;
  autoAcceptedProposalIds: string[];
  errors: CommandValidationError[];
}

export function ingestIncomingProposalsWithPolicy(
  document: WorkspaceDocument,
  proposals: ChartProposalDocument[],
  registry: CommandRegistry = defaultCommandRegistry
): ProposalIngestResult {
  let nextDocument = ingestIncomingProposals(document, proposals);
  const autoAcceptedProposalIds: string[] = [];
  const errors: CommandValidationError[] = [];
  const incomingIds = new Set(proposals.map((proposal) => proposal.id));

  for (const proposal of nextDocument.proposals) {
    if (!incomingIds.has(proposal.id) || proposal.status !== "pending" || proposal.validationErrors.length > 0) continue;
    const panel = nextDocument.panels.find((item) => item.id === proposal.targetPanelId);
    if (panel?.pinMode !== "auto") continue;
    const result = applyCommand(
      nextDocument,
      {
        id: createCommandId("cmd-auto-accept"),
        type: "proposal.accept",
        actor: "system",
        status: "accepted",
        target: {
          workspaceId: nextDocument.id,
          panelId: proposal.targetPanelId,
          chartId: proposal.targetChartId
        },
        payload: { proposalId: proposal.id },
        createdAt: nowIso()
      },
      registry
    );
    if (result.ok) {
      nextDocument = result.document;
      autoAcceptedProposalIds.push(proposal.id);
    } else {
      errors.push(...result.errors);
    }
  }

  return { document: nextDocument, autoAcceptedProposalIds, errors };
}

function applySingleCommand(
  document: WorkspaceDocument,
  command: Command,
  registry: CommandRegistry,
  forcedChangedPaths?: string[]
): CommandApplyResult {
  const errors = validateCommand(document, command, registry);
  if (errors.length > 0) {
    return {
      ok: false,
      status: "blocked",
      commandId: command.id,
      document,
      errors
    };
  }
  const definition = registry[command.type] as CommandDefinition;
  const beforeVersion = document.version;
  const changedDocument = definition.apply(command, document);
  const changedPaths = forcedChangedPaths ?? inferChangedPaths(command);
  const committed = commit(changedDocument, command, "applied", changedPaths, []);
  return {
    ok: true,
    status: "applied",
    commandId: command.id,
    document: committed,
    diff: {
      beforeVersion,
      afterVersion: committed.version,
      changedPaths
    },
    errors: []
  };
}

function applyAcceptProposal(
  document: WorkspaceDocument,
  command: Extract<Command, { type: "proposal.accept" }>,
  registry: CommandRegistry
): CommandApplyResult {
  const errors = validateCommand(document, command, registry);
  if (errors.length > 0) {
    return { ok: false, status: "blocked", commandId: command.id, document, errors };
  }

  const proposal = document.proposals.find((item) => item.id === command.payload.proposalId);
  if (!proposal) {
    return { ok: false, status: "blocked", commandId: command.id, document, errors: [error("target_not_found", "Proposal was not found.")] };
  }

  const groupId = createCommandId("group");
  const acceptedChildActor = command.actor === "system" ? "ai" : "user";
  const userCommands = proposal.commands.map((proposalCommand) => ({
    ...proposalCommand,
    id: createCommandId("cmd-accepted"),
    actor: acceptedChildActor,
    status: "accepted",
    createdAt: nowIso()
  })) as Command[];

  let stagedDocument = document;
  const changedPaths = new Set<string>([`/proposals/${proposal.id}/status`]);
  for (const childCommand of userCommands) {
    const childErrors = validateCommand(stagedDocument, childCommand, registry);
    if (childErrors.length > 0) {
      return { ok: false, status: "blocked", commandId: command.id, document, errors: childErrors };
    }
    const definition = registry[childCommand.type] as CommandDefinition;
    stagedDocument = definition.apply(childCommand, stagedDocument);
    inferChangedPaths(childCommand).forEach((path) => changedPaths.add(path));
  }

  const accepted = {
    ...stagedDocument,
    proposals: stagedDocument.proposals.map((item) => (item.id === proposal.id ? { ...item, status: "accepted" as const } : item))
  };
  const changedPathList = Array.from(changedPaths);
  const committed = commit(accepted, command, "applied", changedPathList, [], {
    groupId,
    proposalId: proposal.id
  });
  return {
    ok: true,
    status: "applied",
    commandId: command.id,
    document: committed,
    diff: { beforeVersion: document.version, afterVersion: committed.version, changedPaths: changedPathList },
    errors: []
  };
}

function applyRejectProposal(
  document: WorkspaceDocument,
  command: Extract<Command, { type: "proposal.reject" }>,
  registry: CommandRegistry
): CommandApplyResult {
  const errors = validateCommand(document, command, registry);
  if (errors.length > 0) {
    return { ok: false, status: "blocked", commandId: command.id, document, errors };
  }
  const changedPaths = [`/proposals/${command.payload.proposalId}/status`];
  const rejected = {
    ...document,
    proposals: document.proposals.map((item) =>
      item.id === command.payload.proposalId ? { ...item, status: "rejected" as const } : item
    )
  };
  const committed = commit(rejected, command, "applied", changedPaths, [], { proposalId: command.payload.proposalId });
  return {
    ok: true,
    status: "applied",
    commandId: command.id,
    document: committed,
    diff: { beforeVersion: document.version, afterVersion: committed.version, changedPaths },
    errors: []
  };
}

function commit(
  document: WorkspaceDocument,
  command: Command,
  status: CommandJournalEntry["status"],
  changedPaths: string[],
  errors: CommandValidationError[],
  metadata: Pick<CommandJournalEntry, "groupId" | "proposalId"> = {}
): WorkspaceDocument {
  const journalEntry: CommandJournalEntry = {
    id: createCommandId("journal"),
    commandId: command.id,
    actor: command.actor,
    status,
    changedPaths,
    errors,
    createdAt: nowIso(),
    ...metadata
  };
  return {
    ...document,
    version: document.version + 1,
    updatedAt: nowIso(),
    commandJournal: [...document.commandJournal, journalEntry]
  };
}

function inferChangedPaths(command: Command): string[] {
  if (command.type.startsWith("chart.")) {
    return [`/charts/${command.target.chartId}`];
  }
  if (command.type.startsWith("proposal.")) {
    return [`/proposals/${"proposalId" in command.payload ? command.payload.proposalId : ""}`];
  }
  if (command.type === "panel.pinMode.set") {
    return [`/panels/${command.payload.panelId}/pinMode`];
  }
  if (command.type === "panel.chartTool.set" || command.type === "panel.crosshair.set") {
    return [`/panels/${command.payload.panelId}/config`];
  }
  return ["/"];
}

function error(code: CommandValidationError["code"], message: string, path?: string): CommandValidationError {
  return { code, message, path };
}

export function getChartPanel(document: WorkspaceDocument): PanelDocument | undefined {
  return document.panels.find((panel) => panel.id === document.activePanelId && panel.type === "chart");
}
