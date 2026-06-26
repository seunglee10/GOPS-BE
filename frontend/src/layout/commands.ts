import { getPanelDefinition, panelTypes, resolvePanelVariant } from "./panelRegistry";
import { applyBoundaryResize, applyPanelMoveWithPacking, reflowLayout, validatePlacement } from "./reflow";
import { createDefaultLayoutRecords, createPanelInstance, createPresetLayout, createSeedLayout } from "./seed";
import { createSavedLayoutRecord, loadSavedLayouts, persistSavedLayouts } from "./storage";
import type {
  CommandActor,
  CommandJournalEntry,
  DefaultLayoutKey,
  FavoriteLayoutSlot,
  LayoutCommand,
  LayoutCommandType,
  LayoutProposal,
  LayoutRuntimeState,
  PanelInstance,
  PanelPlacement,
  PanelType,
  RuntimeError,
  SavedLayoutRecord,
  WorkspaceLayout
} from "./types";

const proposedCommands = new Set<LayoutCommandType>([
  "layout.panel.add",
  "layout.panel.remove",
  "layout.panel.move",
  "layout.boundary.resize",
  "layout.panel.replace",
  "layout.panel.pin",
  "layout.panel.unpin",
  "layout.reflow"
]);

export const MAX_USER_LAYOUTS = 4;

const now = () => new Date().toISOString();

function cloneLayout(layout: WorkspaceLayout): WorkspaceLayout {
  return structuredClone(layout) as WorkspaceLayout;
}

function withCurrentRuntimeSettings(layout: WorkspaceLayout, current: WorkspaceLayout): WorkspaceLayout {
  return {
    ...cloneLayout(layout),
    settings: current.settings
  };
}

function addJournal(
  state: LayoutRuntimeState,
  command: LayoutCommand,
  status: CommandJournalEntry["status"],
  message: string
): LayoutRuntimeState {
  const entry: CommandJournalEntry = {
    id: `${command.id}-${status}`,
    commandType: command.type,
    actor: command.actor,
    status,
    message,
    createdAt: now()
  };

  return { ...state, journal: [entry, ...state.journal].slice(0, 20) };
}

function addError(state: LayoutRuntimeState, message: string): LayoutRuntimeState {
  const error: RuntimeError = {
    id: `error-${crypto.randomUUID()}`,
    message,
    createdAt: now()
  };

  return { ...state, errors: [error, ...state.errors].slice(0, 8) };
}

function fail(state: LayoutRuntimeState, command: LayoutCommand, message: string): LayoutRuntimeState {
  return addError(addJournal(state, command, "failed", message), message);
}

function withLayoutHistory(
  state: LayoutRuntimeState,
  command: LayoutCommand,
  nextLayout: WorkspaceLayout,
  message: string,
  includeHistory = true
): LayoutRuntimeState {
  const history = includeHistory ? [...state.history, cloneLayout(state.layout)].slice(-50) : state.history;
  return addJournal(
    {
      ...state,
      layout: nextLayout,
      history,
      future: includeHistory ? [] : state.future
    },
    command,
    "applied",
    message
  );
}

function buildProposal(command: LayoutCommand): LayoutProposal {
  return {
    id: command.proposalId ?? `proposal-${crypto.randomUUID()}`,
    title: "LLM layout proposal",
    rationale: "LLM actor proposed a layout change. Auto apply is off, so this waits for review.",
    commands: [command],
    createdAt: now()
  };
}

function normalizePanel(panel: PanelInstance): PanelInstance {
  const definition = getPanelDefinition(panel.type);
  const withWeight = {
    ...panel,
    title: panel.title ?? definition.title,
    layoutWeight: panel.layoutWeight ?? definition.defaultWeight
  };

  return {
    ...withWeight,
    variant: resolvePanelVariant(withWeight),
    updatedAt: now()
  };
}

function isPanelType(value: unknown): value is PanelType {
  return typeof value === "string" && panelTypes.includes(value as PanelType);
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function readRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? { ...(value as Record<string, unknown>) }
    : {};
}

function readFavoriteSlot(value: unknown): FavoriteLayoutSlot | null {
  return value === 1 || value === 2 || value === 3 || value === 4 ? value : null;
}

function readDefaultLayoutKey(value: unknown): DefaultLayoutKey | null {
  return value === "chart" || value === "news" || value === "overview" || value === "signals" ? value : null;
}

function findPanel(layout: WorkspaceLayout, panelId: unknown): PanelInstance | null {
  if (typeof panelId !== "string") {
    return null;
  }

  return layout.panels.find((panel) => panel.id === panelId) ?? null;
}

function updatePanel(layout: WorkspaceLayout, panel: PanelInstance): WorkspaceLayout {
  return {
    ...layout,
    panels: layout.panels.map((item) => (item.id === panel.id ? normalizePanel(panel) : item))
  };
}

function validateWholeLayout(layout: WorkspaceLayout): string | null {
  const result = reflowLayout(layout);
  return result.ok ? null : result.message;
}

function panelSnapshot(panel: PanelInstance) {
  return {
    id: panel.id,
    type: panel.type,
    title: panel.title,
    placement: panel.placement,
    props: panel.props,
    resourceRefs: panel.resourceRefs,
    chartDocumentId: panel.chartDocumentId,
    layoutPinned: Boolean(panel.layoutPinned)
  };
}

export function layoutSnapshotsEqual(a: WorkspaceLayout, b: WorkspaceLayout): boolean {
  const aPanels = [...a.panels].map(panelSnapshot).sort((left, right) => left.id.localeCompare(right.id));
  const bPanels = [...b.panels].map(panelSnapshot).sort((left, right) => left.id.localeCompare(right.id));
  return JSON.stringify({ panels: aPanels, selectedPanelId: a.selectedPanelId }) ===
    JSON.stringify({ panels: bPanels, selectedPanelId: b.selectedPanelId });
}

function mergeDefaultAndStoredLayouts(stored: SavedLayoutRecord[]): SavedLayoutRecord[] {
  const defaults = createDefaultLayoutRecords();
  const mergedDefaults = defaults.map((defaultRecord) => {
    const storedDefault = stored.find(
      (record) => record.kind === "default" && record.defaultKey === defaultRecord.defaultKey
    );
    return storedDefault ?? defaultRecord;
  });
  const users = stored.filter((record) => record.kind === "user").slice(0, MAX_USER_LAYOUTS);
  return [...mergedDefaults, ...users];
}

function persistLayouts(records: SavedLayoutRecord[]): string | null {
  return persistSavedLayouts(records);
}

function applyAdd(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const panelType = command.payload.panelType;
  if (!isPanelType(panelType)) {
    return fail(state, command, "Unknown panel type.");
  }

  const definition = getPanelDefinition(panelType);
  const placement = (command.payload.placement as PanelPlacement | undefined) ?? definition.defaultPlacement;
  const props = readRecord(command.payload.props);
  const panel = createPanelInstance(panelType, placement, command.actor, props, `panel-${panelType}-${crypto.randomUUID()}`);
  const validation = validatePlacement(state.layout, panel, placement);
  if (validation) {
    return fail(state, command, validation);
  }

  const nextLayout = {
    ...state.layout,
    panels: [...state.layout.panels, panel],
    selectedPanelId: panel.id
  };
  const reflowResult = reflowLayout(nextLayout);

  if (!reflowResult.ok) {
    return fail(state, command, "Panel add placement is not available.");
  }

  return withLayoutHistory(state, command, nextLayout, `${definition.title} panel added.`);
}

function applyRemove(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const panel = findPanel(state.layout, command.target?.panelId ?? command.payload.panelId);
  if (!panel) {
    return fail(state, command, "Panel not found.");
  }

  const nextLayout = {
    ...state.layout,
    panels: state.layout.panels.filter((item) => item.id !== panel.id),
    selectedPanelId: state.layout.selectedPanelId === panel.id ? undefined : state.layout.selectedPanelId
  };

  return withLayoutHistory(state, command, nextLayout, `${panel.title ?? panel.id} removed.`);
}

function applyMove(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const panel = findPanel(state.layout, command.target?.panelId ?? command.payload.panelId);
  if (!panel) {
    return fail(state, command, "Panel not found.");
  }

  const partial = command.payload.placement as Partial<PanelPlacement> | undefined;
  const requestedPlacement: PanelPlacement = {
    ...panel.placement,
    ...partial,
    ...(typeof command.payload.col === "number" ? { col: command.payload.col } : {}),
    ...(typeof command.payload.row === "number" ? { row: command.payload.row } : {}),
    ...(typeof command.payload.colSpan === "number" ? { colSpan: command.payload.colSpan } : {}),
    ...(typeof command.payload.rowSpan === "number" ? { rowSpan: command.payload.rowSpan } : {})
  };

  const result = applyPanelMoveWithPacking(state.layout, panel.id, requestedPlacement);
  if (!result.ok) {
    return fail(state, command, result.message);
  }

  if (layoutSnapshotsEqual(state.layout, result.layout)) {
    return state;
  }

  return withLayoutHistory(state, command, result.layout, result.message);
}

function applyBoundaryResizeCommand(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const axis = command.payload.axis;
  const line = command.payload.line;
  const segmentStart = command.payload.segmentStart;
  const segmentSpan = command.payload.segmentSpan;
  const delta = command.payload.delta;

  if (
    (axis !== "x" && axis !== "y") ||
    typeof line !== "number" ||
    typeof segmentStart !== "number" ||
    typeof segmentSpan !== "number" ||
    typeof delta !== "number"
  ) {
    return fail(state, command, "Boundary resize payload is invalid.");
  }

  const result = applyBoundaryResize(state.layout, axis, line, segmentStart, segmentSpan, delta);
  if (!result.ok) {
    return fail(state, command, result.message);
  }

  if (layoutSnapshotsEqual(state.layout, result.layout)) {
    return state;
  }

  return withLayoutHistory(state, command, result.layout, result.message);
}

function applyReplace(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const panel = findPanel(state.layout, command.target?.panelId ?? command.payload.panelId);
  if (!panel) {
    return fail(state, command, "Panel not found.");
  }

  const nextType = command.payload.panelType;
  if (!isPanelType(nextType)) {
    return fail(state, command, "Unknown replacement panel type.");
  }

  if (panel.layoutPinned) {
    return fail(state, command, "Pinned panels cannot be replaced.");
  }

  if (panel.type === nextType) {
    return state;
  }

  const props = readRecord(command.payload.props);
  const replacement = createPanelInstance(nextType, panel.placement, command.actor, props, panel.id);
  const validation = validatePlacement(state.layout, replacement);
  if (validation) {
    return fail(state, command, validation);
  }

  const nextLayout = updatePanel(state.layout, replacement);
  return withLayoutHistory(state, command, nextLayout, `${panel.title ?? panel.id} replaced with ${replacement.title}.`);
}

function applyPin(state: LayoutRuntimeState, command: LayoutCommand, value: boolean): LayoutRuntimeState {
  const panel = findPanel(state.layout, command.target?.panelId ?? command.payload.panelId);
  if (!panel) {
    return fail(state, command, "Panel not found.");
  }

  const nextPanel = { ...panel, layoutPinned: value, updatedAt: now() };
  const nextLayout = updatePanel(state.layout, nextPanel);
  return withLayoutHistory(state, command, nextLayout, `${nextPanel.title ?? nextPanel.id} ${value ? "pinned" : "unpinned"}.`);
}

function applySelect(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  if (command.payload.clear === true) {
    if (!state.layout.selectedPanelId) {
      return state;
    }

    return addJournal(
      {
        ...state,
        layout: { ...state.layout, selectedPanelId: undefined }
      },
      command,
      "applied",
      "Panel selection cleared."
    );
  }

  const panel = findPanel(state.layout, command.target?.panelId ?? command.payload.panelId);
  if (!panel) {
    return fail(state, command, "Panel not found.");
  }

  return addJournal(
    {
      ...state,
      layout: { ...state.layout, selectedPanelId: panel.id }
    },
    command,
    "applied",
    `${panel.title ?? panel.id} selected.`
  );
}

function applyReflow(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const result = reflowLayout(state.layout);
  if (!result.ok) {
    return fail(state, command, result.message);
  }

  if (layoutSnapshotsEqual(state.layout, result.layout)) {
    return state;
  }

  return withLayoutHistory(state, command, result.layout, result.message);
}

function applyUndo(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const previous = state.history[state.history.length - 1];
  if (!previous) {
    return fail(state, command, "Nothing to undo.");
  }

  return addJournal(
    {
      ...state,
      layout: withCurrentRuntimeSettings(previous, state.layout),
      history: state.history.slice(0, -1),
      future: [cloneLayout(state.layout), ...state.future].slice(0, 50)
    },
    command,
    "undone",
    "Layout undo applied."
  );
}

function applyRedo(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const next = state.future[0];
  if (!next) {
    return fail(state, command, "Nothing to redo.");
  }

  return addJournal(
    {
      ...state,
      layout: withCurrentRuntimeSettings(next, state.layout),
      history: [...state.history, cloneLayout(state.layout)].slice(-50),
      future: state.future.slice(1)
    },
    command,
    "redone",
    "Layout redo applied."
  );
}

function applySave(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const userCount = state.savedLayouts.filter((record) => record.kind === "user").length;
  if (userCount >= MAX_USER_LAYOUTS) {
    return fail(state, command, "User layout limit is 4.");
  }

  const name = readString(command.payload.name) ?? `User Layout ${userCount + 1}`;
  const record = createSavedLayoutRecord(name, state.layout);
  const savedLayouts = [...state.savedLayouts, record];
  const storageError = persistLayouts(savedLayouts);
  const next = addJournal({ ...state, savedLayouts }, command, "applied", `${name} saved.`);

  return storageError ? addError(next, storageError) : next;
}

function applyUpdate(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const savedLayoutId = readString(command.payload.savedLayoutId);
  if (!savedLayoutId) {
    return fail(state, command, "Saved layout id is missing.");
  }

  const target = state.savedLayouts.find((record) => record.id === savedLayoutId);
  if (!target) {
    return fail(state, command, "Saved layout not found.");
  }

  const savedLayouts = state.savedLayouts.map((record) =>
    record.id === savedLayoutId
      ? { ...record, layout: cloneLayout(state.layout), savedAt: now() }
      : record
  );
  const storageError = persistLayouts(savedLayouts);
  const next = addJournal({ ...state, savedLayouts }, command, "applied", `${target.name} updated.`);

  return storageError ? addError(next, storageError) : next;
}

function applyDelete(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const savedLayoutId = readString(command.payload.savedLayoutId);
  if (!savedLayoutId) {
    return fail(state, command, "Saved layout id is missing.");
  }

  const target = state.savedLayouts.find((record) => record.id === savedLayoutId);
  if (!target) {
    return fail(state, command, "Saved layout not found.");
  }

  if (target.kind === "default") {
    return fail(state, command, "Default layouts cannot be deleted.");
  }

  const savedLayouts = state.savedLayouts.filter((record) => record.id !== savedLayoutId);
  const storageError = persistLayouts(savedLayouts);
  const next = addJournal({ ...state, savedLayouts }, command, "applied", `${target.name} deleted.`);

  return storageError ? addError(next, storageError) : next;
}

function applyFavoriteSet(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const savedLayoutId = readString(command.payload.savedLayoutId);
  if (!savedLayoutId) {
    return fail(state, command, "Saved layout id is missing.");
  }

  const requestedSlot = command.payload.favoriteSlot === null ? null : readFavoriteSlot(command.payload.favoriteSlot);
  if (requestedSlot === null && command.payload.favoriteSlot !== null) {
    return fail(state, command, "Favorite slot must be 1-4 or null.");
  }

  const targetExists = state.savedLayouts.some((record) => record.id === savedLayoutId);
  if (!targetExists) {
    return fail(state, command, "Saved layout not found.");
  }

  const savedLayouts = state.savedLayouts.map((record) => {
    if (record.id === savedLayoutId) {
      return { ...record, favoriteSlot: requestedSlot ?? undefined };
    }

    if (requestedSlot && record.favoriteSlot === requestedSlot) {
      return { ...record, favoriteSlot: undefined };
    }

    return record;
  });

  const storageError = persistLayouts(savedLayouts);
  const message = requestedSlot ? `Saved layout pinned to favorite ${requestedSlot}.` : "Saved layout removed from favorites.";
  const next = addJournal({ ...state, savedLayouts }, command, "applied", message);

  return storageError ? addError(next, storageError) : next;
}

function applyLoad(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const savedId = readString(command.payload.savedLayoutId);
  if (!savedId) {
    return fail(state, command, "No saved layout selected.");
  }

  const record = state.savedLayouts.find((item) => item.id === savedId);
  if (!record) {
    return fail(state, command, "Saved layout not found.");
  }

  const nextLayout = withCurrentRuntimeSettings(record.layout, state.layout);
  const validation = validateWholeLayout(nextLayout);
  if (validation) {
    return fail(state, command, `Invalid saved layout: ${validation}`);
  }

  return withLayoutHistory(state, command, nextLayout, `${record.name} loaded.`);
}

function applyReset(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  return withLayoutHistory(state, command, withCurrentRuntimeSettings(createSeedLayout(), state.layout), "Seed layout restored.");
}

function applyDefaultRestore(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const defaultKey = readDefaultLayoutKey(command.payload.defaultKey);
  if (!defaultKey) {
    return fail(state, command, "Default layout key is missing.");
  }

  const restoredLayout = createPresetLayout(defaultKey);
  const savedLayouts = state.savedLayouts.map((record) =>
    record.kind === "default" && record.defaultKey === defaultKey
      ? { ...record, layout: restoredLayout, savedAt: now() }
      : record
  );
  const storageError = persistLayouts(savedLayouts);
  const next = addJournal({ ...state, savedLayouts }, command, "applied", `${defaultKey} default restored.`);

  return storageError ? addError(next, storageError) : next;
}

function applyAutoSet(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const value = readBoolean(command.payload.value);
  if (value === null) {
    return fail(state, command, "Auto apply value must be boolean.");
  }

  const nextLayout = {
    ...state.layout,
    settings: { ...state.layout.settings, llmLayoutAutoApply: value }
  };

  return withLayoutHistory(state, command, nextLayout, `LLM layout auto apply ${value ? "enabled" : "disabled"}.`, false);
}

function applyProposalAccept(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const proposalId = readString(command.payload.proposalId);
  if (!proposalId) {
    return fail(state, command, "Proposal id is missing.");
  }

  const proposal = state.pendingProposals.find((item) => item.id === proposalId);
  if (!proposal) {
    return fail(state, command, "Proposal not found.");
  }

  let nextState: LayoutRuntimeState = {
    ...state,
    pendingProposals: state.pendingProposals.filter((item) => item.id !== proposal.id)
  };

  for (const childCommand of proposal.commands) {
    nextState = executeCommand(nextState, childCommand, { forceApplyLlm: true });
    if (nextState.journal[0]?.status === "failed") {
      return fail(state, command, "Proposal failed to apply atomically.");
    }
  }

  return addJournal(nextState, command, "applied", `${proposal.title} accepted.`);
}

function applyProposalReject(state: LayoutRuntimeState, command: LayoutCommand): LayoutRuntimeState {
  const proposalId = readString(command.payload.proposalId);
  if (!proposalId) {
    return fail(state, command, "Proposal id is missing.");
  }

  return addJournal(
    {
      ...state,
      pendingProposals: state.pendingProposals.filter((item) => item.id !== proposalId)
    },
    command,
    "applied",
    "Proposal rejected."
  );
}

export function makeCommand(
  type: LayoutCommandType,
  actor: CommandActor,
  payload: Record<string, unknown> = {},
  target?: LayoutCommand["target"]
): LayoutCommand {
  return {
    id: `cmd-${crypto.randomUUID()}`,
    type,
    actor,
    target,
    payload,
    createdAt: now()
  };
}

export function createInitialRuntimeState(): LayoutRuntimeState {
  const { records, error } = loadSavedLayouts();
  const seed = createSeedLayout();
  const savedLayouts = mergeDefaultAndStoredLayouts(records);
  const state: LayoutRuntimeState = {
    layout: seed,
    history: [],
    future: [],
    journal: [],
    errors: [],
    pendingProposals: [],
    savedLayouts
  };

  return error ? addError(state, error) : state;
}

export function executeCommand(
  state: LayoutRuntimeState,
  command: LayoutCommand,
  options: { forceApplyLlm?: boolean } = {}
): LayoutRuntimeState {
  if (
    command.actor === "llm" &&
    !options.forceApplyLlm &&
    !state.layout.settings.llmLayoutAutoApply &&
    proposedCommands.has(command.type)
  ) {
    const proposal = buildProposal(command);
    return addJournal(
      { ...state, pendingProposals: [proposal, ...state.pendingProposals] },
      command,
      "proposed",
      "LLM layout command is waiting for approval."
    );
  }

  switch (command.type) {
    case "layout.panel.add":
      return applyAdd(state, command);
    case "layout.panel.remove":
      return applyRemove(state, command);
    case "layout.panel.move":
      return applyMove(state, command);
    case "layout.boundary.resize":
      return applyBoundaryResizeCommand(state, command);
    case "layout.panel.replace":
      return applyReplace(state, command);
    case "layout.panel.pin":
      return applyPin(state, command, true);
    case "layout.panel.unpin":
      return applyPin(state, command, false);
    case "layout.panel.select":
      return applySelect(state, command);
    case "layout.reflow":
      return applyReflow(state, command);
    case "layout.undo":
      return applyUndo(state, command);
    case "layout.redo":
      return applyRedo(state, command);
    case "layout.save":
      return applySave(state, command);
    case "layout.update":
      return applyUpdate(state, command);
    case "layout.delete":
      return applyDelete(state, command);
    case "layout.load":
      return applyLoad(state, command);
    case "layout.favorite.set":
      return applyFavoriteSet(state, command);
    case "layout.default.restore":
      return applyDefaultRestore(state, command);
    case "layout.reset":
      return applyReset(state, command);
    case "layout.autoApply.set":
      return applyAutoSet(state, command);
    case "layout.proposal.accept":
      return applyProposalAccept(state, command);
    case "layout.proposal.reject":
      return applyProposalReject(state, command);
    default:
      return fail(state, command, "Unsupported command.");
  }
}
