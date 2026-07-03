import { applyCandleEvent, applySnapshotToCandles, candleKey } from "./candleStore";
import { createChartDocument } from "./chartDocuments";
import { normalizeChartInterval } from "./intervals";
import { DEFAULT_CHART_SYMBOL } from "./symbols";
import {
  executeChartCommand,
  executeChartCommandGroup,
  normalizeComparisonFromCommand,
  normalizeComparisonSeries,
  normalizeDrawingFromCommand,
  normalizeDrawingEntity,
  validateChartProposal
} from "./commands";
import type {
  CandleEvent,
  CandleSnapshot,
  ChartCommand,
  ChartCommandType,
  ChartCommandJournalEntry,
  ChartDataStatus,
  ChartDocument,
  ChartPendingPreview,
  ChartProposal,
  ChartRuntimeError,
  ChartRuntimeState,
  StreamStatus
} from "./types";

export type { ChartRuntimeState } from "./types";

export type ChartRuntimePanel = {
  id: string;
  type: string;
  props: Record<string, unknown>;
  chartDocumentId?: string;
};

export type ChartRuntimeAction =
  | { kind: "chart.ensureDocuments"; panels: ChartRuntimePanel[] }
  | { kind: "chart.snapshot.loaded"; snapshot: CandleSnapshot }
  | { kind: "chart.snapshot.failed"; symbol: string; interval: string; message: string }
  | { kind: "chart.command"; command: ChartCommand }
  | { kind: "chart.command.group"; commands: ChartCommand[]; label: string; proposalId?: string }
  | { kind: "chart.proposal.received"; proposal: ChartProposal; autoApply: boolean }
  | { kind: "chart.proposal.accept"; proposalId: string }
  | { kind: "chart.proposal.reject"; proposalId: string }
  | { kind: "chart.live"; event: CandleEvent }
  | { kind: "chart.data.status"; symbol: string; interval: string; status: Omit<ChartDataStatus, "updatedAt"> }
  | { kind: "chart.stream.status"; symbol: string; interval: string; status: StreamStatus; message?: string }
  | { kind: "chart.error"; message: string; chartDocumentId?: string };

export function createInitialChartRuntimeState(): ChartRuntimeState {
  return {
    documents: {},
    candlesByKey: {},
    dataStatusByKey: {},
    streamStatusByKey: {},
    streamMessageByKey: {},
    pendingPreviewByDocumentId: {},
    pendingProposals: [],
    journal: [],
    errors: []
  };
}

export function chartRuntimeReducer(state: ChartRuntimeState, action: ChartRuntimeAction): ChartRuntimeState {
  switch (action.kind) {
    case "chart.ensureDocuments":
      return ensureChartDocuments(state, action.panels);
    case "chart.snapshot.loaded":
      return applySnapshot(state, action.snapshot);
    case "chart.snapshot.failed":
      return setDataStatus(state, action.symbol, action.interval, {
        state: "error",
        message: action.message,
        updatedAt: now()
      });
    case "chart.command":
      return applyCommand(state, action.command);
    case "chart.command.group":
      return applyCommandGroup(state, action.commands, action.label, action.proposalId);
    case "chart.proposal.received":
      return receiveProposal(state, action.proposal, action.autoApply);
    case "chart.proposal.accept":
      return acceptProposal(state, action.proposalId);
    case "chart.proposal.reject":
      return rejectProposal(state, action.proposalId);
    case "chart.live":
      return applyLiveEvent(state, action.event);
    case "chart.data.status":
      return setDataStatus(state, action.symbol, action.interval, {
        ...action.status,
        updatedAt: now()
      });
    case "chart.stream.status":
      return setStreamStatus(state, action.symbol, action.interval, action.status, action.message);
    case "chart.error":
      return fail(state, action.message, action.chartDocumentId);
    default:
      return state;
  }
}

export function getChartDocumentForPanel(state: ChartRuntimeState, panel: ChartRuntimePanel): ChartDocument {
  const chartDocumentId = getChartDocumentId(panel);
  return state.documents[chartDocumentId] ?? createChartDocument(chartDocumentId, readPanelSymbol(panel), readPanelTimeframe(panel));
}

export function getChartDocumentId(panel: ChartRuntimePanel): string {
  return panel.chartDocumentId ?? `${panel.id}-chartDocument`;
}

export function getCandlesForDocument(state: ChartRuntimeState, document: ChartDocument) {
  return state.candlesByKey[candleKey(document.symbol, document.timeframe)] ?? [];
}

export function getDataStatusForDocument(state: ChartRuntimeState, document: ChartDocument): ChartDataStatus {
  return state.dataStatusByKey[candleKey(document.symbol, document.timeframe)] ?? {
    state: "loading",
    message: "Loading candles",
    updatedAt: now()
  };
}

export function getStreamStatusForDocument(state: ChartRuntimeState, document: ChartDocument): StreamStatus {
  return state.streamStatusByKey[candleKey(document.symbol, document.timeframe)] ?? "connecting";
}

export function getStreamMessageForDocument(state: ChartRuntimeState, document: ChartDocument): string | undefined {
  return state.streamMessageByKey?.[candleKey(document.symbol, document.timeframe)];
}

function ensureChartDocuments(state: ChartRuntimeState, panels: ChartRuntimePanel[]): ChartRuntimeState {
  const chartPanels = panels.filter((panel) => panel.type === "chart");
  const activeDocumentIds = new Set(chartPanels.map(getChartDocumentId));
  const documents: ChartRuntimeState["documents"] = {};
  let changed = Object.keys(state.documents).length !== activeDocumentIds.size;

  for (const panel of chartPanels) {
    const id = getChartDocumentId(panel);
    const document = state.documents[id] ?? createChartDocument(id, readPanelSymbol(panel), readPanelTimeframe(panel));
    documents[id] = document;
    if (!state.documents[id]) {
      changed = true;
    }
  }

  const pendingPreviewByDocumentId = Object.fromEntries(
    Object.entries(state.pendingPreviewByDocumentId).filter(([documentId]) => activeDocumentIds.has(documentId))
  );
  if (Object.keys(pendingPreviewByDocumentId).length !== Object.keys(state.pendingPreviewByDocumentId).length) {
    changed = true;
  }

  const pendingProposals = state.pendingProposals.filter((proposal) => activeDocumentIds.has(proposal.target.chartDocumentId));
  if (pendingProposals.length !== state.pendingProposals.length) {
    changed = true;
  }

  const errors = state.errors.filter((error) => !error.chartDocumentId || activeDocumentIds.has(error.chartDocumentId));
  if (errors.length !== state.errors.length) {
    changed = true;
  }

  return changed ? { ...state, documents, pendingPreviewByDocumentId, pendingProposals, errors } : state;
}

function applySnapshot(state: ChartRuntimeState, snapshot: CandleSnapshot): ChartRuntimeState {
  const key = candleKey(snapshot.symbol, snapshot.interval);
  const current = state.candlesByKey[key] ?? [];
  const previousStatus = state.dataStatusByKey[key];
  const isEmptyRangePage = current.length > 0 && snapshot.candles.length === 0 && Boolean(snapshot.requestedRange);
  const dataState = isEmptyRangePage
    ? previousStatus?.state ?? "ready"
    : snapshot.dataStatus ?? (snapshot.candles.length ? "ready" : "empty");
  const preservesHistoryAvailability = isEmptyRangePage && !snapshot.noDataBefore && previousStatus?.hasMoreBefore === true;
  const message = isEmptyRangePage
    ? previousStatus?.message
    : snapshot.message ?? (dataState === "empty" ? "No candle data" : undefined);
  return {
    ...state,
    candlesByKey: { ...state.candlesByKey, [key]: applySnapshotToCandles(snapshot, current) },
    dataStatusByKey: {
      ...state.dataStatusByKey,
      [key]: {
        state: dataState,
        message,
        source: snapshot.source,
        feed: snapshot.feed,
        feedProfile: snapshot.feedProfile,
        marketSession: snapshot.marketSession,
        backfillStatus: snapshot.backfillStatus ?? "not_requested",
        repairStatus: snapshot.repairStatus,
        canBackfill: snapshot.canBackfill ?? false,
        sourceInterval: snapshot.sourceInterval,
        requestedLimit: snapshot.requestedLimit,
        returnedCount: snapshot.returnedCount,
        storedCandleCount: snapshot.storedCandleCount,
        availableFrom: snapshot.availableFrom,
        availableTo: snapshot.availableTo,
        noDataBefore: snapshot.noDataBefore,
        requestedRange: snapshot.requestedRange,
        oldestTimestamp: snapshot.oldestTimestamp,
        newestTimestamp: snapshot.newestTimestamp,
        hasMoreBefore: preservesHistoryAvailability ? true : snapshot.hasMoreBefore,
        hasMoreAfter: snapshot.hasMoreAfter,
        coverage: snapshot.coverage,
        updatedAt: now()
      }
    },
    journal: addJournal(state.journal, "chart.data.snapshot", "system", "applied", `${snapshot.symbol} ${snapshot.interval} snapshot loaded.`)
  };
}

function applyLiveEvent(state: ChartRuntimeState, event: CandleEvent): ChartRuntimeState {
  const key = candleKey(event.symbol, event.interval);
  const current = state.candlesByKey[key] ?? [];
  const result = applyCandleEvent(current, event);
  const previousStatus = state.dataStatusByKey[key];
  if (!result.applied) {
    return {
      ...state,
      journal: addJournal(state.journal, "chart.data.live", "system", "ignored", result.message),
      streamStatusByKey: { ...state.streamStatusByKey, [key]: "stale" }
    };
  }

  const appendedCount = Math.max(0, result.candles.length - current.length);

  return {
    ...state,
    candlesByKey: { ...state.candlesByKey, [key]: result.candles },
    documents: appendedCount > 0
      ? freezeDetachedViewports(state.documents, event.symbol, event.interval, appendedCount)
      : state.documents,
    dataStatusByKey: {
      ...state.dataStatusByKey,
      [key]: {
        ...previousStatus,
        state: previousStatus?.state === "partial" && previousStatus.coverage?.renderable !== true ? "partial" : "ready",
        source: event.source ?? previousStatus?.source,
        feed: event.feed ?? previousStatus?.feed,
        feedProfile: event.feedProfile ?? event.data.feedProfile ?? previousStatus?.feedProfile,
        marketSession: event.marketSession ?? event.data.marketSession ?? previousStatus?.marketSession,
        sourceInterval: event.sourceInterval ?? event.data.sourceInterval ?? previousStatus?.sourceInterval,
        updatedAt: now()
      }
    },
    streamStatusByKey: { ...state.streamStatusByKey, [key]: "live" },
    journal: addJournal(state.journal, "chart.data.live", "system", "applied", result.message)
  };
}

function freezeDetachedViewports(
  documents: ChartRuntimeState["documents"],
  symbol: string,
  interval: string,
  appendedCount: number
): ChartRuntimeState["documents"] {
  let changed = false;
  const next: ChartRuntimeState["documents"] = {};
  Object.entries(documents).forEach(([id, document]) => {
    if (
      document.symbol !== symbol ||
      document.timeframe !== interval ||
      document.viewport.rightOffset <= 0
    ) {
      next[id] = document;
      return;
    }
    changed = true;
    next[id] = {
      ...document,
      viewport: {
        ...document.viewport,
        rightOffset: document.viewport.rightOffset + appendedCount
      },
      updatedAt: now()
    };
  });
  return changed ? next : documents;
}

function applyCommand(state: ChartRuntimeState, command: ChartCommand): ChartRuntimeState {
  if (command.type.startsWith("chart.preview.")) {
    return applyPreviewCommand(state, command);
  }

  const document = state.documents[command.target.chartDocumentId];
  if (!document) {
    return fail(state, `Chart document not found: ${command.target.chartDocumentId}.`, command.target.chartDocumentId);
  }

  const result = executeChartCommand(document, command);
  if (!result.ok) {
    return fail(
      { ...state, journal: addJournal(state.journal, command.type, command.actor, "failed", result.message, document.id) },
      result.message,
      document.id
    );
  }

  if (result.noOp) {
    return state;
  }

  return {
    ...state,
    documents: { ...state.documents, [document.id]: result.document },
    journal: addJournal(state.journal, command.type, command.actor, "applied", result.message, document.id)
  };
}

function applyCommandGroup(
  state: ChartRuntimeState,
  commands: ChartCommand[],
  label: string,
  proposalId?: string
): ChartRuntimeState {
  if (commands.length === 0) {
    return state;
  }
  const documentId = commands[0]?.target.chartDocumentId;
  const document = documentId ? state.documents[documentId] : undefined;
  if (!document) {
    return fail(state, "Chart document not found.", documentId);
  }

  const result = executeChartCommandGroup(document, commands, label, proposalId);
  if (!result.ok) {
    return fail(
      { ...state, journal: addJournal(state.journal, commands[0].type, commands[0].actor, "failed", result.message, document.id) },
      result.message,
      document.id
    );
  }
  if (result.noOp) {
    return state;
  }
  return {
    ...state,
    documents: { ...state.documents, [document.id]: result.document },
    journal: addJournal(state.journal, commands[0].type, commands[0].actor, "applied", result.message, document.id)
  };
}

function receiveProposal(state: ChartRuntimeState, proposal: ChartProposal, autoApply: boolean): ChartRuntimeState {
  const validation = validateChartProposal(proposal);
  if (validation) {
    return fail(state, validation, proposal.target.chartDocumentId);
  }

  if (proposal.commands.some(isPreviewFirstCommand)) {
    return setPendingPreviewFromProposal(state, proposal);
  }

  if (!autoApply) {
    return {
      ...state,
      pendingProposals: [{ ...proposal, status: "pending" as const }, ...state.pendingProposals].slice(0, 8),
      journal: addJournal(state.journal, "chart.proposal.accept", "llm", "proposed", proposal.title, proposal.target.chartDocumentId)
    };
  }

  return applyProposal(state, proposal, "applied");
}

function acceptProposal(state: ChartRuntimeState, proposalId: string): ChartRuntimeState {
  const proposal = state.pendingProposals.find((item) => item.id === proposalId);
  if (!proposal) {
    return fail(state, "Chart proposal not found.");
  }
  return applyProposal(state, proposal, "applied");
}

function rejectProposal(state: ChartRuntimeState, proposalId: string): ChartRuntimeState {
  const proposal = state.pendingProposals.find((item) => item.id === proposalId);
  const nextPreviews = { ...state.pendingPreviewByDocumentId };
  if (proposal) {
    const preview = nextPreviews[proposal.target.chartDocumentId];
    if (preview?.sourceProposalId === proposalId) {
      delete nextPreviews[proposal.target.chartDocumentId];
    }
  }
  return {
    ...state,
    pendingPreviewByDocumentId: nextPreviews,
    pendingProposals: state.pendingProposals.filter((item) => item.id !== proposalId),
    journal: proposal
      ? addJournal(state.journal, "chart.proposal.reject", "user", "applied", `Rejected: ${proposal.title}`, proposal.target.chartDocumentId)
      : state.journal
  };
}

function applyProposal(state: ChartRuntimeState, proposal: ChartProposal, status: "applied"): ChartRuntimeState {
  const document = state.documents[proposal.target.chartDocumentId];
  if (!document) {
    return fail(state, "Chart document not found.", proposal.target.chartDocumentId);
  }

  const result = executeChartCommandGroup(document, proposal.commands, proposal.title, proposal.id);
  if (!result.ok) {
    return fail(
      {
        ...state,
        pendingProposals: state.pendingProposals.map((item) =>
          item.id === proposal.id ? { ...item, status: "failed" as const, error: result.message } : item
        )
      },
      result.message,
      document.id
    );
  }

  if (result.noOp) {
    return {
      ...state,
      pendingProposals: state.pendingProposals.filter((item) => item.id !== proposal.id)
    };
  }

  return {
    ...state,
    documents: { ...state.documents, [document.id]: result.document },
    pendingProposals: state.pendingProposals.filter((item) => item.id !== proposal.id),
    journal: addJournal(state.journal, "chart.proposal.accept", "llm", status, result.message, document.id)
  };
}

function applyPreviewCommand(state: ChartRuntimeState, command: ChartCommand): ChartRuntimeState {
  switch (command.type) {
    case "chart.preview.set": {
      const preview = normalizePreviewPayload(command.payload.preview, command.target.chartDocumentId, command.actor, command.proposalId);
      if (!preview) {
        return fail(state, "Invalid chart preview payload.", command.target.chartDocumentId);
      }
      return {
        ...state,
        pendingPreviewByDocumentId: {
          ...state.pendingPreviewByDocumentId,
          [command.target.chartDocumentId]: preview
        },
        journal: addJournal(state.journal, command.type, command.actor, "applied", "Chart preview set.", command.target.chartDocumentId)
      };
    }
    case "chart.preview.toggle": {
      const preview = state.pendingPreviewByDocumentId[command.target.chartDocumentId];
      if (!preview) {
        return state;
      }
      const visible = typeof command.payload.previewVisible === "boolean" ? command.payload.previewVisible : !preview.visible;
      return {
        ...state,
        pendingPreviewByDocumentId: {
          ...state.pendingPreviewByDocumentId,
          [command.target.chartDocumentId]: { ...preview, visible }
        },
        journal: addJournal(state.journal, command.type, command.actor, "applied", visible ? "Chart preview shown." : "Chart preview hidden.", command.target.chartDocumentId)
      };
    }
    case "chart.preview.clear": {
      if (!state.pendingPreviewByDocumentId[command.target.chartDocumentId]) {
        return state;
      }
      const nextPreviews = { ...state.pendingPreviewByDocumentId };
      delete nextPreviews[command.target.chartDocumentId];
      return {
        ...state,
        pendingPreviewByDocumentId: nextPreviews,
        journal: addJournal(state.journal, command.type, command.actor, "applied", "Chart preview cleared.", command.target.chartDocumentId)
      };
    }
    case "chart.preview.apply": {
      const preview = state.pendingPreviewByDocumentId[command.target.chartDocumentId];
      const document = state.documents[command.target.chartDocumentId];
      if (!preview || !document) {
        return fail(state, "No chart preview to apply.", command.target.chartDocumentId);
      }
      if (!preview.visible) {
        return fail(state, "Hidden chart preview cannot be applied.", command.target.chartDocumentId);
      }
      const commands = [
        ...preview.drawings.map((drawing) => ({
          ...command,
          id: `chart-command-${crypto.randomUUID()}`,
          type: "chart.drawing.add" as const,
          actor: command.actor,
          payload: { drawing: { ...drawing, createdBy: command.actor, sourceProposalId: preview.sourceProposalId } },
          proposalId: preview.sourceProposalId
        })),
        ...preview.comparisons.map((comparison) => ({
          ...command,
          id: `chart-command-${crypto.randomUUID()}`,
          type: "chart.comparison.add" as const,
          actor: command.actor,
          payload: { comparison },
          proposalId: preview.sourceProposalId
        }))
      ];
      const result = executeChartCommandGroup(document, commands, "Apply chart preview", preview.sourceProposalId);
      if (!result.ok) {
        return fail(state, result.message, document.id);
      }
      const nextPreviews = { ...state.pendingPreviewByDocumentId };
      delete nextPreviews[document.id];
      if (result.noOp) {
        return { ...state, pendingPreviewByDocumentId: nextPreviews };
      }
      return {
        ...state,
        documents: { ...state.documents, [document.id]: result.document },
        pendingPreviewByDocumentId: nextPreviews,
        pendingProposals: state.pendingProposals.filter((item) => item.id !== preview.sourceProposalId),
        journal: addJournal(state.journal, command.type, command.actor, "applied", result.message, document.id)
      };
    }
    default:
      return state;
  }
}

function setPendingPreviewFromProposal(state: ChartRuntimeState, proposal: ChartProposal): ChartRuntimeState {
  const preview = buildPreviewFromProposal(proposal);
  if (!preview) {
    return fail(state, "Preview proposal did not include drawable content.", proposal.target.chartDocumentId);
  }

  return {
    ...state,
    pendingPreviewByDocumentId: {
      ...state.pendingPreviewByDocumentId,
      [proposal.target.chartDocumentId]: preview
    },
    pendingProposals: [{ ...proposal, status: "pending" as const }, ...state.pendingProposals.filter((item) => item.target.chartDocumentId !== proposal.target.chartDocumentId)].slice(0, 8),
    journal: addJournal(state.journal, "chart.preview.set", "llm", "proposed", proposal.title, proposal.target.chartDocumentId)
  };
}

function buildPreviewFromProposal(proposal: ChartProposal): ChartPendingPreview | null {
  const drawings = proposal.commands.flatMap((command) => {
    const drawing = normalizeDrawingFromCommand(command);
    return drawing ? [drawing] : [];
  });
  const comparisons = proposal.commands.flatMap((command) => {
    const comparison = normalizeComparisonFromCommand(command);
    return comparison ? [comparison] : [];
  });
  const preview = normalizePreviewPayload({
    id: `preview-${proposal.id}`,
    sourceProposalId: proposal.id,
    drawings,
    comparisons,
    rationale: proposal.rationale,
    confidence: 0.72
  }, proposal.target.chartDocumentId, "llm", proposal.id);
  return preview && (preview.drawings.length > 0 || preview.comparisons.length > 0) ? preview : null;
}

function normalizePreviewPayload(
  value: unknown,
  chartDocumentId: string,
  actor: ChartCommand["actor"],
  proposalId?: string
): ChartPendingPreview | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const source = value as Record<string, unknown>;
  const nowTime = now();
  const drawings = Array.isArray(source.drawings)
    ? source.drawings
      .map((item) => normalizeDrawingEntity(item, actor, proposalId))
      .filter((item): item is ChartPendingPreview["drawings"][number] => Boolean(item))
    : [];
  const comparisons = Array.isArray(source.comparisons)
    ? source.comparisons
      .map(normalizeComparisonSeries)
      .filter((item): item is ChartPendingPreview["comparisons"][number] => Boolean(item))
    : [];
  return {
    id: typeof source.id === "string" && source.id.trim() ? source.id : `chart-preview-${chartDocumentId}-${crypto.randomUUID()}`,
    sourceProposalId: typeof source.sourceProposalId === "string" ? source.sourceProposalId : proposalId,
    drawings,
    comparisons,
    rationale: typeof source.rationale === "string" ? source.rationale : undefined,
    confidence: typeof source.confidence === "number" ? source.confidence : undefined,
    visible: typeof source.visible === "boolean" ? source.visible : true,
    createdAt: nowTime
  };
}

function isPreviewFirstCommand(command: ChartCommand): boolean {
  return command.type.startsWith("chart.drawing.") ||
    command.type.startsWith("chart.comparison.") ||
    command.type === "chart.measurement.add";
}

function setDataStatus(
  state: ChartRuntimeState,
  symbol: string,
  interval: string,
  status: ChartDataStatus
): ChartRuntimeState {
  const key = candleKey(symbol, interval);
  return {
    ...state,
    dataStatusByKey: { ...state.dataStatusByKey, [key]: status }
  };
}

function setStreamStatus(
  state: ChartRuntimeState,
  symbol: string,
  interval: string,
  status: StreamStatus,
  message?: string
): ChartRuntimeState {
  const key = candleKey(symbol, interval);
  const streamMessageByKey = { ...(state.streamMessageByKey ?? {}) };
  if (message) {
    streamMessageByKey[key] = message;
  } else {
    delete streamMessageByKey[key];
  }
  return {
    ...state,
    streamStatusByKey: { ...state.streamStatusByKey, [key]: status },
    streamMessageByKey,
    journal: message ? addJournal(state.journal, "chart.data.live", "system", status === "error" ? "failed" : "applied", message) : state.journal
  };
}

function fail(state: ChartRuntimeState, message: string, chartDocumentId?: string): ChartRuntimeState {
  return {
    ...state,
    errors: addError(state.errors, message, chartDocumentId)
  };
}

function addJournal(
  journal: ChartCommandJournalEntry[],
  commandType: ChartCommandJournalEntry["commandType"],
  actor: ChartCommandJournalEntry["actor"],
  status: ChartCommandJournalEntry["status"],
  message: string,
  chartDocumentId?: string
): ChartCommandJournalEntry[] {
  return [
    {
      id: `chart-journal-${crypto.randomUUID()}`,
      commandType,
      actor,
      status,
      message,
      chartDocumentId,
      createdAt: now()
    },
    ...journal
  ].slice(0, 30);
}

function addError(errors: ChartRuntimeError[], message: string, chartDocumentId?: string): ChartRuntimeError[] {
  return [
    {
      id: `chart-error-${crypto.randomUUID()}`,
      message,
      chartDocumentId,
      createdAt: now()
    },
    ...errors
  ].slice(0, 10);
}

function readPanelSymbol(panel: ChartRuntimePanel): string {
  return typeof panel.props.symbol === "string" && panel.props.symbol.trim()
    ? panel.props.symbol.trim().toUpperCase()
    : DEFAULT_CHART_SYMBOL;
}

function readPanelTimeframe(panel: ChartRuntimePanel): string {
  return normalizeChartInterval(panel.props.timeframe) ?? "1m";
}

function now() {
  return new Date().toISOString();
}
