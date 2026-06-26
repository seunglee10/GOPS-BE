import assert from "node:assert/strict";
import { getChartAgentAccess } from "../src/chart/agentAccess";
import { normalizeAgentChatResponse } from "../src/chart/agentChat";
import { applyCandleEvent, candleKey } from "../src/chart/candleStore";
import { createChartDocument } from "../src/chart/chartDocuments";
import { executeChartCommand, executeChartCommandGroup, makeChartCommand, validateChartProposal } from "../src/chart/commands";
import { normalizeCandleSnapshot } from "../src/chart/marketDataAdapter";
import { buildRenderScene } from "../src/chart/renderScene";
import { chartRuntimeReducer, createInitialChartRuntimeState } from "../src/chart/runtime";
import { createCoordinateTransform } from "../src/chart/scales";
import { normalizeSupportedSymbol, normalizeWatchlistPayload } from "../src/chart/symbols";
import type { CandleData, ChartPendingPreview, ChartProposal } from "../src/chart/types";
import {
  createInitialRuntimeState as createInitialLayoutRuntimeState,
  executeCommand as executeLayoutCommand,
  makeCommand as makeLayoutCommand
} from "../src/layout/commands";
import {
  createPanelDropCommand,
  findMaxEmptyWorkspaceRect,
  findWorkspacePanelAtCell,
  getWorkspaceDropCell
} from "../src/layout/panelCatalogDrop";
import { createPanelInstance } from "../src/layout/seed";
import type { PanelInstance, PanelPlacement, PanelType, WorkspaceLayout } from "../src/layout/types";
import { clampRightOffset, dragDeltaToRightOffset } from "../src/chart/viewport";

function target(panelId: string, chartDocumentId: string) {
  return { panelId, chartDocumentId };
}

function chartPanel(panelId: string, chartDocumentId: string, symbol = "AAPL"): PanelInstance {
  return {
    id: panelId,
    type: "chart",
    title: "Chart",
    placement: { group: "workspace", zone: "main", col: 1, row: 1, colSpan: 1, rowSpan: 1 },
    props: { symbol },
    chartDocumentId,
    variant: "standard",
    createdBy: "system",
    updatedAt: "2026-06-26T00:00:00.000Z"
  };
}

function testPlacement(col: number, row: number, colSpan = 1, rowSpan = 1): PanelPlacement {
  return {
    group: "workspace",
    zone: col === 4 && colSpan === 1 ? "context" : col + colSpan - 1 <= 3 ? "main" : "mainContext",
    col,
    row,
    colSpan,
    rowSpan
  };
}

function testPanel(id: string, type: PanelType, placement: PanelPlacement, pinned = false): PanelInstance {
  return {
    ...createPanelInstance(type, placement, "system", {}, id),
    layoutPinned: pinned
  };
}

function testLayout(panels: PanelInstance[], selectedPanelId = panels[0]?.id): WorkspaceLayout {
  return {
    version: 1,
    zones: {
      workspace: { columns: 4, rows: 5, mainColumns: 3, contextColumns: 1 },
      agentRail: { columns: 1, rows: 5 }
    },
    settings: {
      llmLayoutAutoApply: false,
      reflowMode: "auto"
    },
    panels,
    selectedPanelId
  };
}

function filledLayoutExcept(emptyCells: string[]): WorkspaceLayout {
  const empty = new Set(emptyCells);
  const panels: PanelInstance[] = [];
  for (let row = 1; row <= 5; row += 1) {
    for (let col = 1; col <= 4; col += 1) {
      if (!empty.has(`${col}:${row}`)) {
        panels.push(testPanel(`panel-${col}-${row}`, "aiSummary", testPlacement(col, row)));
      }
    }
  }
  return testLayout(panels);
}

function pickPlacement(placement?: PanelPlacement | null) {
  if (!placement) {
    return null;
  }

  return {
    col: placement.col,
    row: placement.row,
    colSpan: placement.colSpan,
    rowSpan: placement.rowSpan
  };
}

const documentA = createChartDocument("chart-doc-a", "AAPL", "1m");
const viewportCommand = makeChartCommand("chart.viewport.set", "user", target("panel-a", documentA.id), {
  visibleCount: 42,
  rightOffset: 3
});
const viewportResult = executeChartCommand(documentA, viewportCommand);

assert.equal(viewportResult.ok, true);
if (viewportResult.ok) {
  assert.equal(viewportResult.document.viewport.visibleCount, 42);
  assert.equal(viewportResult.document.viewport.rightOffset, 3);
  assert.equal(viewportResult.document.history.length, 1);

  const undoResult = executeChartCommand(
    viewportResult.document,
    makeChartCommand("chart.undo", "user", target("panel-a", documentA.id))
  );
  assert.equal(undoResult.ok, true);
  if (undoResult.ok) {
    assert.equal(undoResult.document.viewport.visibleCount, documentA.viewport.visibleCount);
    assert.equal(undoResult.document.future.length, 1);
  }
}

const noOpResult = executeChartCommand(
  documentA,
  makeChartCommand("chart.viewport.set", "user", target("panel-a", documentA.id), documentA.viewport)
);
assert.equal(noOpResult.ok, true);
if (noOpResult.ok) {
  assert.equal(noOpResult.noOp, true);
  assert.equal(noOpResult.document.history.length, 0);
}

const unsupportedSymbolResult = executeChartCommand(
  documentA,
  makeChartCommand("chart.symbol.set", "user", target("panel-a", documentA.id), { symbol: "GOOG" })
);
assert.equal(unsupportedSymbolResult.ok, false);

const supportedSymbolResult = executeChartCommand(
  documentA,
  makeChartCommand("chart.symbol.set", "user", target("panel-a", documentA.id), { symbol: "NVDA" })
);
assert.equal(supportedSymbolResult.ok, true);
if (supportedSymbolResult.ok) {
  assert.equal(supportedSymbolResult.document.symbol, "NVDA");
}

const documentB = createChartDocument("chart-doc-b", "MSFT", "5m");
const documentBResult = executeChartCommand(
  documentB,
  makeChartCommand("chart.layer.visibility.set", "user", target("panel-b", documentB.id), {
    layer: "ma20",
    visible: false
  })
);
assert.equal(documentBResult.ok, true);
assert.equal(documentA.history.length, 0);
if (documentBResult.ok) {
  assert.equal(documentBResult.document.history.length, 1);
}

const candleA: CandleData = {
  timestamp: "2026-06-25T13:30:00Z",
  open: 10,
  high: 11,
  low: 9,
  close: 10.5,
  volume: 100,
  isClosed: true
};
const candleB: CandleData = {
  timestamp: "2026-06-25T13:31:00Z",
  open: 10.5,
  high: 11.3,
  low: 10.1,
  close: 10.8,
  volume: 120,
  isClosed: false
};
const candleC: CandleData = {
  timestamp: "2026-06-25T13:32:00Z",
  open: 10.8,
  high: 11.5,
  low: 10.6,
  close: 11.1,
  volume: 130,
  isClosed: false
};
const correctedA: CandleData = { ...candleA, close: 10.9, high: 11.1 };

const staleResult = applyCandleEvent([candleB], {
  type: "LIVE_CANDLE_UPDATE",
  symbol: "AAPL",
  interval: "1m",
  data: candleA
});
assert.equal(staleResult.applied, false);

const correctedResult = applyCandleEvent([candleA, candleB], {
  type: "CANDLE_CORRECTED",
  symbol: "AAPL",
  interval: "1m",
  data: correctedA
});
assert.equal(correctedResult.applied, true);
assert.equal(correctedResult.candles[0]?.close, correctedA.close);

const liveMutationResult = applyCandleEvent([candleA, candleB], {
  type: "LIVE_CANDLE_UPDATE",
  symbol: "AAPL",
  interval: "1m",
  data: { ...candleB, close: 11.2, high: 11.8, volume: 180 }
});
assert.equal(liveMutationResult.applied, true);
assert.equal(liveMutationResult.candles.length, 2);
assert.equal(liveMutationResult.candles[1]?.close, 11.2);
assert.equal(liveMutationResult.candles[1]?.volume, 180);

const invalidProposal: ChartProposal = {
  id: "proposal-invalid",
  title: "Invalid mixed actor",
  rationale: "Should fail validation",
  summary: "Invalid",
  target: target("panel-a", documentA.id),
  commands: [
    makeChartCommand("chart.viewport.set", "user", target("panel-a", documentA.id), {
      visibleCount: 50
    })
  ],
  insights: [],
  status: "pending",
  createdAt: new Date().toISOString(),
  createdByAgentId: "agent-01"
};

assert.match(validateChartProposal(invalidProposal) ?? "", /llm actor/);

const syntheticSnapshot = normalizeCandleSnapshot({
  symbol: "NVDA",
  interval: "5m",
  source: "dummy",
  feed: "synthetic-demo",
  isSynthetic: true,
  candles: [candleA],
});
assert.equal(syntheticSnapshot.isSynthetic, true);
assert.equal(syntheticSnapshot.feed, "synthetic-demo");

const syntheticRuntime = chartRuntimeReducer(createInitialChartRuntimeState(), {
  kind: "chart.snapshot.loaded",
  snapshot: syntheticSnapshot,
});
assert.equal(syntheticRuntime.dataStatusByKey[candleKey("NVDA", "5m")]?.state, "ready");
assert.equal(syntheticRuntime.candlesByKey[candleKey("NVDA", "5m")]?.length, 1);
const syntheticLiveRuntime = chartRuntimeReducer(syntheticRuntime, {
  kind: "chart.live",
  event: {
    type: "LIVE_CANDLE_UPDATE",
    symbol: "NVDA",
    interval: "5m",
    source: "dummy",
    feed: "synthetic-demo",
    isSynthetic: true,
    data: { ...candleA, close: 11.4, high: 11.6 }
  }
});
assert.equal(syntheticLiveRuntime.dataStatusByKey[candleKey("NVDA", "5m")]?.isSynthetic, true);
assert.equal(syntheticLiveRuntime.dataStatusByKey[candleKey("NVDA", "5m")]?.feed, "synthetic-demo");

const lifecyclePanelA = chartPanel("panel-lifecycle-a", "chart-doc-lifecycle-a", "AAPL");
const lifecyclePanelB = chartPanel("panel-lifecycle-b", "chart-doc-lifecycle-b", "MSFT");
const lifecyclePreview: ChartPendingPreview = {
  id: "preview-lifecycle-b",
  sourceProposalId: "proposal-lifecycle-b",
  drawings: [],
  comparisons: [],
  visible: true,
  createdAt: "2026-06-26T00:00:00.000Z"
};
let lifecycleState = chartRuntimeReducer(createInitialChartRuntimeState(), {
  kind: "chart.ensureDocuments",
  panels: [lifecyclePanelA, lifecyclePanelB]
});
lifecycleState = {
  ...lifecycleState,
  pendingPreviewByDocumentId: { [lifecyclePanelB.chartDocumentId ?? ""]: lifecyclePreview },
  pendingProposals: [{
    id: "proposal-lifecycle-b",
    title: "Stale proposal",
    rationale: "Panel B was removed.",
    summary: "Stale",
    target: target(lifecyclePanelB.id, lifecyclePanelB.chartDocumentId ?? ""),
    commands: [],
    insights: [],
    status: "pending",
    createdAt: "2026-06-26T00:00:00.000Z",
    createdByAgentId: "agent-01"
  }],
  errors: [
    { id: "error-lifecycle-b", message: "Stale chart error", chartDocumentId: lifecyclePanelB.chartDocumentId, createdAt: "2026-06-26T00:00:00.000Z" },
    { id: "error-global", message: "Global error", createdAt: "2026-06-26T00:00:00.000Z" }
  ]
};
const prunedLifecycleState = chartRuntimeReducer(lifecycleState, {
  kind: "chart.ensureDocuments",
  panels: [lifecyclePanelA]
});
assert.ok(prunedLifecycleState.documents[lifecyclePanelA.chartDocumentId ?? ""]);
assert.equal(prunedLifecycleState.documents[lifecyclePanelB.chartDocumentId ?? ""], undefined);
assert.equal(prunedLifecycleState.pendingPreviewByDocumentId[lifecyclePanelB.chartDocumentId ?? ""], undefined);
assert.equal(prunedLifecycleState.pendingProposals.length, 0);
assert.equal(prunedLifecycleState.errors.some((error) => error.id === "error-lifecycle-b"), false);
assert.equal(prunedLifecycleState.errors.some((error) => error.id === "error-global"), true);

assert.equal(normalizeSupportedSymbol(" nvda "), "NVDA");
assert.equal(normalizeSupportedSymbol("GOOG"), null);

const watchlist = normalizeWatchlistPayload({
  symbols: [
    { symbol: "AAPL", name: "Apple", market: "NASDAQ", lastPrice: 190.12, changePercent: 1.2, volume: 1000 },
    { symbol: "GOOG", name: "Alphabet", lastPrice: 1 }
  ]
});
assert.equal(watchlist.length, 5);
assert.equal(watchlist[0]?.symbol, "AAPL");
assert.equal(watchlist[0]?.market, "NASDAQ");
assert.equal(watchlist.find((item) => item.symbol === "SPY")?.market, "NYSEARCA");
assert.equal(watchlist[0]?.lastPrice, 190.12);
assert.equal(watchlist.some((item) => item.symbol === "GOOG"), false);

const frameCell = getWorkspaceDropCell({ left: 10, top: 20, width: 550, height: 500 }, 12, 24);
assert.deepEqual(frameCell, { col: 1, row: 1 });
const systemAreaCell = getWorkspaceDropCell({ left: 0, top: 0, width: 550, height: 500 }, 530, 20);
assert.equal(systemAreaCell, null);

const singleEmptyLayout = filledLayoutExcept(["4:5"]);
const singleEmptyRect = findMaxEmptyWorkspaceRect(singleEmptyLayout, { col: 4, row: 5 });
assert.deepEqual(singleEmptyRect && pickPlacement(singleEmptyRect), { col: 4, row: 5, colSpan: 1, rowSpan: 1 });

const twoByTwoEmptyLayout = filledLayoutExcept(["2:2", "3:2", "2:3", "3:3"]);
const twoByTwoRect = findMaxEmptyWorkspaceRect(twoByTwoEmptyLayout, { col: 2, row: 2 });
assert.deepEqual(twoByTwoRect && pickPlacement(twoByTwoRect), { col: 2, row: 2, colSpan: 2, rowSpan: 2 });

const lShapedEmptyLayout = filledLayoutExcept(["1:1", "2:1", "1:2"]);
const lShapedRect = findMaxEmptyWorkspaceRect(lShapedEmptyLayout, { col: 1, row: 1 });
assert.deepEqual(lShapedRect && pickPlacement(lShapedRect), { col: 1, row: 1, colSpan: 2, rowSpan: 1 });
assert.equal(findWorkspacePanelAtCell(lShapedEmptyLayout, { col: 3, row: 1 })?.id, "panel-3-1");

const emptyDropCommand = createPanelDropCommand({
  layout: singleEmptyLayout,
  panelType: "chart",
  activeSymbol: "NVDA",
  cell: { col: 4, row: 5 }
});
assert.equal(emptyDropCommand?.type, "layout.panel.add");
assert.equal((emptyDropCommand?.payload.props as Record<string, unknown> | undefined)?.symbol, "NVDA");
assert.deepEqual(pickPlacement(emptyDropCommand?.payload.placement as PanelPlacement), { col: 4, row: 5, colSpan: 1, rowSpan: 1 });

const replaceTarget = testPanel("replace-news", "newsFeed", testPlacement(2, 2, 2, 2));
const replaceLayout = testLayout([replaceTarget]);
const replaceCommand = createPanelDropCommand({
  layout: replaceLayout,
  panelType: "chart",
  activeSymbol: "MSFT",
  targetPanelId: replaceTarget.id
});
assert.equal(replaceCommand?.type, "layout.panel.replace");
let replaceState = {
  ...createInitialLayoutRuntimeState(),
  layout: replaceLayout,
  history: [],
  future: [],
  journal: [],
  errors: []
};
replaceState = executeLayoutCommand(replaceState, replaceCommand ?? makeLayoutCommand("layout.reflow", "user"));
assert.equal(replaceState.layout.panels[0]?.id, replaceTarget.id);
assert.equal(replaceState.layout.panels[0]?.type, "chart");
assert.equal(replaceState.layout.panels[0]?.placement.colSpan, 2);
assert.equal(replaceState.layout.panels[0]?.props.symbol, "MSFT");
assert.ok(replaceState.layout.panels[0]?.chartDocumentId);
assert.equal(replaceState.history.length, 1);

const pinnedTarget = testPanel("pinned-news", "newsFeed", testPlacement(1, 1), true);
const pinnedLayout = testLayout([pinnedTarget]);
const pinnedState = executeLayoutCommand(
  {
    ...createInitialLayoutRuntimeState(),
    layout: pinnedLayout,
    history: [],
    future: [],
    journal: [],
    errors: []
  },
  makeLayoutCommand("layout.panel.replace", "user", { panelId: pinnedTarget.id, panelType: "chart", props: { symbol: "TSLA" } })
);
assert.equal(pinnedState.layout.panels[0]?.type, "newsFeed");
assert.equal(pinnedState.history.length, 0);
assert.equal(pinnedState.errors.length, 1);

const sameTypeTarget = testPanel("same-chart", "chart", testPlacement(1, 1), false);
const sameTypeLayout = testLayout([sameTypeTarget]);
const sameTypeState = {
  ...createInitialLayoutRuntimeState(),
  layout: sameTypeLayout,
  history: [],
  future: [],
  journal: [],
  errors: []
};
const sameTypeResult = executeLayoutCommand(
  sameTypeState,
  makeLayoutCommand("layout.panel.replace", "user", { panelId: sameTypeTarget.id, panelType: "chart", props: { symbol: "SPY" } })
);
assert.equal(sameTypeResult.history.length, 0);
assert.equal(sameTypeResult.journal.length, 0);
assert.equal(sameTypeResult.layout.panels[0]?.chartDocumentId, sameTypeTarget.chartDocumentId);

const primaryChart = testPanel("primary-chart", "chart", testPlacement(1, 1, 2, 2), false);
const multiChartLayout = testLayout([primaryChart]);
const multiChartState = executeLayoutCommand(
  {
    ...createInitialLayoutRuntimeState(),
    layout: multiChartLayout,
    history: [],
    future: [],
    journal: [],
    errors: []
  },
  makeLayoutCommand("layout.panel.add", "user", {
    panelType: "chart",
    placement: testPlacement(3, 1, 1, 2),
    props: { symbol: "TSLA" }
  })
);
const chartPanels = multiChartState.layout.panels.filter((panel) => panel.type === "chart");
assert.equal(chartPanels.length, 2);
assert.notEqual(chartPanels[0]?.chartDocumentId, chartPanels[1]?.chartDocumentId);
let multiChartRuntime = chartRuntimeReducer(createInitialChartRuntimeState(), {
  kind: "chart.ensureDocuments",
  panels: multiChartState.layout.panels
});
assert.equal(multiChartRuntime.documents[chartPanels[0]?.chartDocumentId ?? ""]?.symbol, "AAPL");
assert.equal(multiChartRuntime.documents[chartPanels[1]?.chartDocumentId ?? ""]?.symbol, "TSLA");

assert.equal(clampRightOffset(120, 72, 160), 88);
assert.equal(dragDeltaToRightOffset(0, 18, 9, 72, 160), 2);
assert.equal(dragDeltaToRightOffset(8, -27, 9, 72, 160), 5);

const detachedDocument = createChartDocument("chart-doc-detached", "AAPL", "1m");
detachedDocument.viewport = { visibleCount: 1, rightOffset: 1 };
const detachedState = {
  ...createInitialChartRuntimeState(),
  documents: { [detachedDocument.id]: detachedDocument },
  candlesByKey: { [candleKey("AAPL", "1m")]: [candleA, candleB] }
};
const detachedLiveState = chartRuntimeReducer(detachedState, {
  kind: "chart.live",
  event: {
    type: "LIVE_CANDLE_UPDATE",
    symbol: "AAPL",
    interval: "1m",
    data: candleC
  }
});
assert.equal(detachedLiveState.documents[detachedDocument.id]?.viewport.rightOffset, 2);

const followDocument = createChartDocument("chart-doc-follow", "AAPL", "1m");
followDocument.viewport = { visibleCount: 1, rightOffset: 0 };
const followState = {
  ...createInitialChartRuntimeState(),
  documents: { [followDocument.id]: followDocument },
  candlesByKey: { [candleKey("AAPL", "1m")]: [candleA, candleB] }
};
const followLiveState = chartRuntimeReducer(followState, {
  kind: "chart.live",
  event: {
    type: "LIVE_CANDLE_UPDATE",
    symbol: "AAPL",
    interval: "1m",
    data: candleC
  }
});
assert.equal(followLiveState.documents[followDocument.id]?.viewport.rightOffset, 0);

const chatResult = normalizeAgentChatResponse({
  reply: "Applying chart commands.",
  title: "Chat command",
  summary: "Change symbol",
  rationale: "User asked for a symbol change.",
  insights: [],
  commands: [
    {
      type: "chart.symbol.set",
      payload: {
        symbol: "NVDA",
        timeframe: null,
        visibleCount: null,
        rightOffset: null,
        layer: null,
        visible: null
      }
    }
  ]
}, target("panel-a", documentA.id));

assert.equal(chatResult.reply, "Applying chart commands.");
assert.equal(chatResult.proposal?.commands[0]?.type, "chart.symbol.set");
assert.equal(chatResult.proposal?.commands[0]?.actor, "llm");

assert.deepEqual(getChartAgentAccess([{ id: "agent-01" }]), { enabled: true, reason: "agent-01" });
assert.deepEqual(getChartAgentAccess([{ id: "agent-02" }]), { enabled: false, reason: "no-chart-agent" });
assert.deepEqual(getChartAgentAccess([{ id: "agent-01" }, { id: "agent-02" }]), { enabled: false, reason: "orchestration" });

const anchorA = { timestamp: candleA.timestamp, price: 10.4, paneId: "price", symbol: "AAPL", logicalIndex: 0 };
const drawingAddResult = executeChartCommand(
  documentA,
  makeChartCommand("chart.drawing.add", "user", target("panel-a", documentA.id), {
    drawingType: "horizontalLine",
    anchors: [anchorA],
    style: { color: "#111111" },
    label: "Support"
  })
);
assert.equal(drawingAddResult.ok, true);
if (drawingAddResult.ok) {
  assert.equal(drawingAddResult.document.drawings.length, 1);
  assert.equal(drawingAddResult.document.history.length, 1);
  const drawingId = drawingAddResult.document.drawings[0]?.id ?? "";
  const removeResult = executeChartCommand(
    drawingAddResult.document,
    makeChartCommand("chart.drawing.remove", "user", target("panel-a", documentA.id), { drawingId })
  );
  assert.equal(removeResult.ok, true);
  if (removeResult.ok) {
    assert.equal(removeResult.document.drawings.length, 0);
    assert.equal(removeResult.document.history.length, 2);
    const undoDrawing = executeChartCommand(removeResult.document, makeChartCommand("chart.undo", "user", target("panel-a", documentA.id)));
    assert.equal(undoDrawing.ok, true);
    if (undoDrawing.ok) {
      assert.equal(undoDrawing.document.drawings.length, 1);
    }
  }
}

const scopedUndoDocument = createChartDocument("chart-doc-scoped-undo", "AAPL", "1m");
const scopedAdd = executeChartCommand(
  scopedUndoDocument,
  makeChartCommand("chart.drawing.add", "user", target("panel-scoped", scopedUndoDocument.id), {
    drawingType: "horizontalLine",
    anchors: [anchorA],
    style: { color: "#111111" },
    label: "Scoped support"
  }, undefined, "chartPanel")
);
assert.equal(scopedAdd.ok, true);
if (scopedAdd.ok) {
  const externalSymbol = executeChartCommand(
    scopedAdd.document,
    makeChartCommand("chart.symbol.set", "user", target("panel-scoped", scopedUndoDocument.id), { symbol: "NVDA" }, undefined, "external")
  );
  assert.equal(externalSymbol.ok, true);
  if (externalSymbol.ok) {
    assert.equal(externalSymbol.document.symbol, "NVDA");
    assert.equal(externalSymbol.document.history.length, 1);
    const scopedUndo = executeChartCommand(
      externalSymbol.document,
      makeChartCommand("chart.undo", "user", target("panel-scoped", scopedUndoDocument.id))
    );
    assert.equal(scopedUndo.ok, true);
    if (scopedUndo.ok) {
      assert.equal(scopedUndo.document.symbol, "NVDA");
      assert.equal(scopedUndo.document.drawings.length, 0);
    }
  }
}

const clearAllDocument = createChartDocument("chart-doc-clear-all", "AAPL", "1m");
const firstDrawing = makeChartCommand("chart.drawing.add", "user", target("panel-clear", clearAllDocument.id), {
  drawingType: "horizontalLine",
  anchors: [anchorA],
  style: { color: "#111111" },
  label: "Level A"
});
const secondDrawing = makeChartCommand("chart.drawing.add", "user", target("panel-clear", clearAllDocument.id), {
  drawingType: "verticalMarker",
  anchors: [{ timestamp: candleB.timestamp, price: 10.8, paneId: "price", symbol: "AAPL", logicalIndex: 1 }],
  style: { color: "#dc2626" },
  label: "Event B"
});
const seededDrawings = executeChartCommandGroup(clearAllDocument, [firstDrawing, secondDrawing], "Seed drawings");
assert.equal(seededDrawings.ok, true);
if (seededDrawings.ok) {
  assert.equal(seededDrawings.document.drawings.length, 2);
  const removeCommands = seededDrawings.document.drawings.map((drawing) =>
    makeChartCommand("chart.drawing.remove", "user", target("panel-clear", clearAllDocument.id), { drawingId: drawing.id })
  );
  const clearResult = executeChartCommandGroup(seededDrawings.document, removeCommands, "Clear all drawings");
  assert.equal(clearResult.ok, true);
  if (clearResult.ok) {
    assert.equal(clearResult.document.drawings.length, 0);
    assert.equal(clearResult.document.history.length, 2);
    const undoClear = executeChartCommand(clearResult.document, makeChartCommand("chart.undo", "user", target("panel-clear", clearAllDocument.id)));
    assert.equal(undoClear.ok, true);
    if (undoClear.ok) {
      assert.equal(undoClear.document.drawings.length, 2);
    }
  }
}

const isolatedDrawingResult = executeChartCommand(
  documentB,
  makeChartCommand("chart.drawing.add", "user", target("panel-b", documentB.id), {
    drawingType: "verticalMarker",
    anchors: [{ timestamp: candleB.timestamp, price: 10.8, paneId: "price", symbol: "MSFT", logicalIndex: 1 }],
    style: { color: "#dc2626" },
    label: "MSFT event"
  })
);
assert.equal(isolatedDrawingResult.ok, true);
if (isolatedDrawingResult.ok && drawingAddResult.ok) {
  assert.equal(isolatedDrawingResult.document.drawings.length, 1);
  assert.equal(isolatedDrawingResult.document.history.length, 1);
  assert.equal(drawingAddResult.document.drawings.length, 1);
  assert.equal(drawingAddResult.document.drawings[0]?.label, "Support");
  assert.notEqual(isolatedDrawingResult.document.id, drawingAddResult.document.id);
}

const priceOnlyHorizontalLineResult = executeChartCommand(
  documentA,
  makeChartCommand("chart.drawing.add", "llm", target("panel-a", documentA.id), {
    drawingType: "horizontalLine",
    anchors: [{ timestamp: null, price: 141.2, paneId: "price", symbol: "AAPL", logicalIndex: null, value: null }],
    style: { color: "#3b82f6", fillColor: null, lineWidth: 2, textColor: null, lineDash: [] },
    label: "Last 141.20",
    comparison: null,
    comparisonId: null
  })
);
assert.equal(priceOnlyHorizontalLineResult.ok, true);

const comparisonResult = executeChartCommand(
  documentA,
  makeChartCommand("chart.comparison.add", "user", target("panel-a", documentA.id), {
    comparison: {
      id: "comparison-spy-test",
      symbol: "SPY",
      label: "SPY",
      scaleMode: "percent",
      base: { mode: "visibleRangeStart" },
      style: { color: "#0f766e" }
    }
  })
);
assert.equal(comparisonResult.ok, true);
if (comparisonResult.ok) {
  assert.equal(comparisonResult.document.comparisons.length, 1);
}

const nvdaComparisonResult = executeChartCommand(
  documentA,
  makeChartCommand("chart.comparison.add", "user", target("panel-a", documentA.id), {
    comparison: {
      id: "comparison-nvda-test",
      symbol: "NVDA",
      label: "NVDA",
      scaleMode: "percent",
      base: { mode: "visibleRangeStart" },
      style: { color: "#16a34a" }
    }
  })
);
assert.equal(nvdaComparisonResult.ok, true);

const unsupportedComparisonResult = executeChartCommand(
  documentA,
  makeChartCommand("chart.comparison.add", "user", target("panel-a", documentA.id), {
    comparison: {
      id: "comparison-unsupported-test",
      symbol: "GOOG",
      label: "GOOG",
      scaleMode: "percent",
      base: { mode: "visibleRangeStart" },
      style: { color: "#111111" }
    }
  })
);
assert.equal(unsupportedComparisonResult.ok, false);

const sceneForAnchor = buildRenderScene({
  state: "ready",
  document: documentA,
  candles: [candleA, candleB],
  width: 640,
  height: 360,
  comparisonCandlesBySymbol: {
    SPY: [
      { ...candleA, close: 20, open: 20, high: 21, low: 19 },
      { ...candleB, close: 22, open: 20, high: 23, low: 19 }
    ]
  }
});
const transform = createCoordinateTransform(sceneForAnchor);
const anchorPoint = transform.anchorToPoint(anchorA);
assert.ok(anchorPoint);
assert.equal(transform.pointToAnchor(anchorPoint?.x ?? 0, anchorPoint?.y ?? 0, "AAPL")?.timestamp, candleA.timestamp);

const comparisonScene = buildRenderScene({
  state: "ready",
  document: comparisonResult.ok ? comparisonResult.document : documentA,
  candles: [candleA, candleB],
  width: 640,
  height: 360,
  comparisonCandlesBySymbol: {
    SPY: [
      { ...candleA, close: 20, open: 20, high: 21, low: 19 },
      { ...candleB, close: 22, open: 20, high: 23, low: 19 }
    ]
  }
});
assert.equal(comparisonScene.comparisonSeries.length, comparisonResult.ok ? 1 : 0);
assert.equal(comparisonScene.comparisonSeries[0]?.points[1]?.percent, 10);

const previewDocument = createChartDocument("chart-doc-preview", "AAPL", "1m");
let previewState = {
  ...createInitialChartRuntimeState(),
  documents: { [previewDocument.id]: previewDocument }
};
const previewProposal: ChartProposal = {
  id: "proposal-preview",
  title: "Preview drawing",
  rationale: "Show a level before applying.",
  summary: "Preview only",
  target: target("panel-preview", previewDocument.id),
  commands: [
    makeChartCommand("chart.drawing.add", "llm", target("panel-preview", previewDocument.id), {
      drawingType: "horizontalLine",
      anchors: [anchorA],
      style: { color: "#2563eb" },
      label: "Agent level"
    }, "proposal-preview")
  ],
  insights: [],
  status: "pending",
  createdAt: new Date().toISOString(),
  createdByAgentId: "agent-01"
};
previewState = chartRuntimeReducer(previewState, {
  kind: "chart.proposal.received",
  proposal: previewProposal,
  autoApply: true
});
assert.equal(previewState.documents[previewDocument.id]?.drawings.length, 0);
assert.equal(previewState.pendingPreviewByDocumentId[previewDocument.id]?.drawings.length, 1);
previewState = chartRuntimeReducer(previewState, {
  kind: "chart.command",
  command: makeChartCommand("chart.preview.toggle", "user", target("panel-preview", previewDocument.id), { previewVisible: false })
});
assert.equal(previewState.pendingPreviewByDocumentId[previewDocument.id]?.visible, false);
previewState = chartRuntimeReducer(previewState, {
  kind: "chart.command",
  command: makeChartCommand("chart.preview.apply", "user", target("panel-preview", previewDocument.id))
});
assert.equal(previewState.documents[previewDocument.id]?.drawings.length, 0);
assert.ok(previewState.errors.some((error) => /Hidden chart preview/.test(error.message)));
previewState = chartRuntimeReducer(previewState, {
  kind: "chart.command",
  command: makeChartCommand("chart.preview.toggle", "user", target("panel-preview", previewDocument.id), { previewVisible: true })
});
previewState = chartRuntimeReducer(previewState, {
  kind: "chart.command",
  command: makeChartCommand("chart.preview.apply", "user", target("panel-preview", previewDocument.id))
});
assert.equal(previewState.pendingPreviewByDocumentId[previewDocument.id], undefined);
assert.equal(previewState.documents[previewDocument.id]?.drawings.length, 1);
assert.equal(previewState.documents[previewDocument.id]?.history.length, 1);
