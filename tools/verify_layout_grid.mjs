import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import vm from "node:vm";
import ts from "typescript";

const repoRoot = process.cwd();
const nodeRequire = createRequire(import.meta.url);
const grid = loadTsModule("frontend/src/layout/grid.ts");
const panelLayout = loadTsModule("frontend/src/layout/panelLayout.ts");

const {
  defaultChartGridSpan,
  gridBoundaryX,
  gridGutter,
  gridLockMaxWidth,
  isGridLocked,
  normalizeGridSpan,
  panelGridBounds
} = grid;

const {
  canInsertPanelAtBoundary,
  createInitialTiledPanelState,
  detectPanelBoundaries,
  insertPanelAtBoundary,
  insertOptionsForBoundary,
  layoutHasGapsOrOverlaps,
  panelGutter,
  removePanelSlot,
  resizePanelBoundary,
  swapPanelContents,
  workspaceBounds
} = panelLayout;

assert(gridLockMaxWidth === 760, "mobile lock width should be 760px");
assert(gridGutter(600) === 10, "gutter should clamp down to 10px");
assert(gridGutter(1000) === 12, "gutter should be responsive at normal desktop widths");
assert(gridGutter(2000) === 18, "gutter should clamp up to 18px");

assert(gridBoundaryX(0, 1000) === 0, "first boundary should be page left");
assert(gridBoundaryX(1, 1000) === 250, "quarter boundary should be 25%");
assert(gridBoundaryX(2, 1000) === 500, "middle boundary should be 50%");
assert(gridBoundaryX(3, 1000) === 750, "third boundary should be 75%");
assert(gridBoundaryX(4, 1000) === 1000, "last boundary should be page right");

assertDeepEqual(defaultChartGridSpan, { start: 0, end: 4 }, "chart default span");
assertDeepEqual(normalizeGridSpan({ start: 3, end: 3 }), { start: 3, end: 4 }, "invalid equal span should keep one column");
assertDeepEqual(normalizeGridSpan({ start: 4, end: 4 }), { start: 3, end: 4 }, "right-edge invalid span should keep one column");

assertDeepEqual(
  panelGridBounds({ start: 0, end: 4 }, 1000, "normal"),
  { left: 12, right: 988, width: 976 },
  "normal full-width panel should keep page gutters"
);
assertDeepEqual(
  panelGridBounds({ start: 0, end: 4 }, 1000, "flush-at-page-edge"),
  { left: 0, right: 1000, width: 1000 },
  "flush full-width panel should touch page edges"
);

assert(isGridLocked(760), "760px should lock the grid");
assert(!isGridLocked(761), "761px should allow grid resizing");

const viewport = { width: 1200, height: 760 };
const workspace = workspaceBounds(viewport);
const gutter = panelGutter(viewport);
const state = createInitialTiledPanelState(viewport);

assert(state.slots.length === 4, "initial chart page should have one chart and three support panels");
assert(!layoutHasGapsOrOverlaps(state, viewport), "initial layout should have no overlap or invalid gutter");
assertDeepEqual(
  state.slots.map((slot) => state.contents[slot.contentId].kind),
  ["news", "ontology", "companyAnalysis", "chart"],
  "initial content order should place chart last/bottom"
);

const chartSlot = slotByKind(state, "chart");
const newsSlot = slotByKind(state, "news");
const ontologySlot = slotByKind(state, "ontology");
const companySlot = slotByKind(state, "companyAnalysis");
assert(chartSlot.required, "chart slot should be required");
assert(chartSlot.rect.top > newsSlot.rect.top, "chart should be below support panels");
assert(chartSlot.rect.left === workspace.left, "chart should flush to page left");
assert(Math.round(chartSlot.rect.width) === workspace.width, "chart should span workspace width");
assert(Math.round(rectBottom(chartSlot.rect)) === rectBottom(workspace), "initial chart should align to the workspace bottom edge");
assert(Math.round(newsSlot.rect.left - workspace.left) === gutter, "support panels should keep page gutter");
assert(Math.round(ontologySlot.rect.left - rectRight(newsSlot.rect)) === gutter, "support columns should keep gutter");
assert(Math.round(companySlot.rect.left - rectRight(ontologySlot.rect)) === gutter, "support columns should keep second gutter");
assert(Math.round(chartSlot.rect.top - rectBottom(newsSlot.rect)) === gutter, "support row and chart should keep gutter");

const boundaries = detectPanelBoundaries(state, viewport);
const chartSupportBoundary = boundaries.find((boundary) => (
  boundary.kind === "shared" &&
  boundary.orientation === "horizontal" &&
  boundary.negativeSlotIds.length === 3 &&
  boundary.positiveSlotIds.includes(chartSlot.id)
));
assert(chartSupportBoundary, "chart/support guide should be detected as one n:m gutter guide");
assert(chartSupportBoundary.interaction === "resize", "chart/support shared guide should be resize-capable");
assert(Math.round(chartSupportBoundary.position) === Math.round(rectBottom(newsSlot.rect) + gutter / 2), "chart/support guide should sit at gutter center");

const topOuterGuide = boundaries.find((boundary) => (
  boundary.kind === "outer" &&
  boundary.orientation === "horizontal" &&
  boundary.negativeSlotIds.length === 0 &&
  boundary.positiveSlotIds.length === 3
));
assert(topOuterGuide, "support row should expose an outer top insertion guide");
assert(topOuterGuide.interaction === "insert-only", "outer top guide should be insert-only");
assert(Math.round(topOuterGuide.position) === Math.round(workspace.top + gutter / 2), "outer guide should sit at page gutter center");
assertDeepEqual(resizePanelBoundary(state, topOuterGuide.id, 32, viewport), state, "outer insertion guide drag should not change layout");

const chartLeftOuterGuide = boundaries.find((boundary) => (
  boundary.orientation === "vertical" &&
  boundary.negativeSlotIds.length === 0 &&
  boundary.positiveSlotIds.includes(chartSlot.id)
));
assert(chartLeftOuterGuide, "full-width chart should expose a virtual left page-edge guide");
assert(chartLeftOuterGuide.pageEdge === "left", "left chart guide should be marked as a page edge");
assert(chartLeftOuterGuide.interaction === "insert-only", "left chart page-edge guide should be insert-only");
assert(canInsertPanelAtBoundary(state, chartLeftOuterGuide.id, viewport), "left page-edge guide should allow insertion");
assertDeepEqual(resizePanelBoundary(state, chartLeftOuterGuide.id, 32, viewport), state, "left page-edge guide drag should not change layout");

const chartRightOuterGuide = boundaries.find((boundary) => (
  boundary.orientation === "vertical" &&
  boundary.pageEdge === "right" &&
  boundary.negativeSlotIds.includes(chartSlot.id)
));
assert(chartRightOuterGuide, "full-width chart should expose a virtual right page-edge guide");
assert(chartRightOuterGuide.interaction === "insert-only", "right chart page-edge guide should be insert-only");
assert(canInsertPanelAtBoundary(state, chartRightOuterGuide.id, viewport), "right page-edge guide should allow insertion");

const chartBottomOuterGuide = boundaries.find((boundary) => (
  boundary.orientation === "horizontal" &&
  boundary.kind === "outer" &&
  boundary.pageEdge === "bottom" &&
  boundary.negativeSlotIds.includes(chartSlot.id) &&
  boundary.positiveSlotIds.length === 0
));
assert(chartBottomOuterGuide, "initial chart should expose a bottom insertion guide when aligned to workspace bottom");
assert(chartBottomOuterGuide.interaction === "insert-only", "bottom chart page-edge guide should be insert-only");
assert(canInsertPanelAtBoundary(state, chartBottomOuterGuide.id, viewport), "bottom chart page-edge guide should allow insertion when chart height has capacity");
assertDeepEqual(resizePanelBoundary(state, chartBottomOuterGuide.id, -32, viewport), state, "bottom page-edge guide drag should not create empty space");

const withBottomTrade = insertPanelAtBoundary(state, chartBottomOuterGuide.id, "trade", viewport);
assert(withBottomTrade.slots.length === 5, "bottom insertion should add a panel");
const bottomTrade = slotByKind(withBottomTrade, "trade");
const chartAboveBottomTrade = defaultChartSlot(withBottomTrade);
assert(Math.round(bottomTrade.rect.top) > Math.round(rectBottom(chartAboveBottomTrade.rect)), "bottom inserted panel should sit below chart");
assert(Math.round(bottomTrade.rect.top - rectBottom(chartAboveBottomTrade.rect)) === gutter, "bottom insertion should keep a gutter below chart");
assert(Math.round(rectBottom(workspace) - rectBottom(bottomTrade.rect)) === gutter, "bottom inserted panel should keep the workspace bottom safe line");
assert(Math.round(bottomTrade.rect.left - workspace.left) === gutter, "bottom inserted panel should keep the left safe line");
assert(Math.round(rectRight(workspace) - rectRight(bottomTrade.rect)) === gutter, "bottom inserted panel should keep the right safe line");
assert(!layoutHasGapsOrOverlaps(withBottomTrade, viewport), "bottom panel insertion should preserve valid gutters");

const withBottomChart = insertPanelAtBoundary(state, chartBottomOuterGuide.id, "chart", viewport, { symbol: "GOOGL" });
assert(withBottomChart.slots.length === 5, "bottom chart insertion should add a chart");
const bottomChart = chartSlotBySymbol(withBottomChart, "GOOGL");
const chartAboveBottomChart = defaultChartSlot(withBottomChart);
assert(Math.round(bottomChart.rect.left) === Math.round(chartSlot.rect.left), "bottom chart should inherit source chart left");
assert(Math.round(rectRight(bottomChart.rect)) === Math.round(rectRight(chartSlot.rect)), "bottom chart should inherit source chart right");
assert(Math.round(rectBottom(bottomChart.rect)) === rectBottom(workspace), "bottom chart should remain flush to workspace bottom");
assert(Math.round(bottomChart.rect.top - rectBottom(chartAboveBottomChart.rect)) === gutter, "bottom chart insertion should keep a gutter below the source chart");
assert(!layoutHasGapsOrOverlaps(withBottomChart, viewport), "bottom chart insertion should preserve valid layout");

const shortBottomState = {
  contents: state.contents,
  nextInstance: state.nextInstance,
  slots: [{
    ...chartSlot,
    rect: {
      left: workspace.left,
      top: rectBottom(workspace) - (chartMinHeightForTest() + panelMinHeightForTest() + gutter),
      width: workspace.width,
      height: chartMinHeightForTest() + panelMinHeightForTest() + gutter
    }
  }]
};
const shortBottomGuide = detectPanelBoundaries(shortBottomState, viewport).find((boundary) => (
  boundary.orientation === "horizontal" &&
  boundary.pageEdge === "bottom" &&
  boundary.negativeSlotIds.includes(chartSlot.id)
));
assert(shortBottomGuide, "short chart fixture should still expose the bottom insert-only guide");
assert(!canInsertPanelAtBoundary(shortBottomState, shortBottomGuide.id, viewport, "trade"), "bottom guide should hide add when chart cannot shrink enough for a panel");

const resized = resizePanelBoundary(state, chartSupportBoundary.id, 32, viewport);
assert(Math.round(slotByKind(resized, "chart").rect.top - chartSlot.rect.top) === 32, "horizontal guide drag should move chart top");
assert(Math.round(slotByKind(resized, "news").rect.height - newsSlot.rect.height) === 32, "horizontal guide drag should resize support row");
assert(Math.round(slotByKind(resized, "chart").rect.top - rectBottom(slotByKind(resized, "news").rect)) === gutter, "resize should preserve gutter");
assert(!layoutHasGapsOrOverlaps(resized, viewport), "resized layout should preserve valid gutters");

assert(canInsertPanelAtBoundary(state, chartSupportBoundary.id, viewport), "chart/support guide should allow insertion");
assert(insertOptionsForBoundary(state, chartSupportBoundary.id, viewport).some((option) => option.kind === "trade"), "trade should be insertable");
const withTrade = insertPanelAtBoundary(state, chartSupportBoundary.id, "trade", viewport);
assert(withTrade.slots.length === 5, "inserting a panel should add a slot");
const tradeSlot = slotByKind(withTrade, "trade");
assert(tradeSlot.rect.top > rectBottom(slotByKind(withTrade, "news").rect), "inserted trade panel should sit below support row");
assert(slotByKind(withTrade, "chart").rect.top > rectBottom(tradeSlot.rect), "chart should sit below inserted trade panel");
assert(Math.round(tradeSlot.rect.left - workspace.left) === gutter, "inserted trade panel should keep the left safe line");
assert(Math.round(rectRight(workspace) - rectRight(tradeSlot.rect)) === gutter, "inserted trade panel should keep the right safe line");
assert(Math.round(tradeSlot.rect.top - rectBottom(slotByKind(withTrade, "news").rect)) === gutter, "insert should keep upper gutter");
assert(Math.round(slotByKind(withTrade, "chart").rect.top - rectBottom(tradeSlot.rect)) === gutter, "insert should keep lower gutter");
assert(!layoutHasGapsOrOverlaps(withTrade, viewport), "inserted layout should preserve valid gutters");

const deletedTrade = removePanelSlot(withTrade, tradeSlot.id, viewport);
assert(deletedTrade.slots.length === 4, "deleting inserted support panel should remove a slot");
assert(!layoutHasGapsOrOverlaps(deletedTrade, viewport), "deleted layout should preserve valid gutters");

const withLeftEdgePanel = insertPanelAtBoundary(state, chartLeftOuterGuide.id, "trade", viewport);
assert(withLeftEdgePanel.slots.length === 5, "left page-edge insertion should add a panel");
const leftEdgeTrade = slotByKind(withLeftEdgePanel, "trade");
const leftEdgeChart = slotByKind(withLeftEdgePanel, "chart");
assert(Math.round(leftEdgeTrade.rect.left - workspace.left) === gutter, "left edge panel should keep the page gutter");
assert(Math.round(leftEdgeChart.rect.left - rectRight(leftEdgeTrade.rect)) === gutter, "left edge panel and chart should keep a gutter");
assert(Math.round(leftEdgeTrade.rect.top) === workspace.top + gutter, "left edge panel should use the full workspace top");
assert(Math.round(rectBottom(leftEdgeTrade.rect)) === rectBottom(workspace) - gutter, "left edge panel should use the full workspace bottom");
assert(!layoutHasGapsOrOverlaps(withLeftEdgePanel, viewport), "left page-edge insertion should preserve valid layout");
const leftEdgeVerticalBoundary = detectPanelBoundaries(withLeftEdgePanel, viewport).find((boundary) => (
  boundary.kind === "shared" &&
  boundary.orientation === "vertical" &&
  boundary.negativeSlotIds.includes(leftEdgeTrade.id) &&
  boundary.positiveSlotIds.includes(leftEdgeChart.id)
));
assert(leftEdgeVerticalBoundary, "left edge insertion should create a shared vertical boundary");
assert(leftEdgeVerticalBoundary.interaction === "resize", "left edge shared vertical boundary should be resize-capable");
const resizedLeftEdge = resizePanelBoundary(withLeftEdgePanel, leftEdgeVerticalBoundary.id, -24, viewport);
assert(slotByKind(resizedLeftEdge, "trade").rect.width < leftEdgeTrade.rect.width, "shared left edge boundary drag should resize the inserted panel when capacity exists");
assert(slotByKind(resizedLeftEdge, "chart").rect.left < leftEdgeChart.rect.left, "shared left edge boundary drag should move the chart when capacity exists");
assert(!layoutHasGapsOrOverlaps(resizedLeftEdge, viewport), "shared edge resize should preserve a valid layout");
const leftEdgeChartSupportBoundary = detectPanelBoundaries(withLeftEdgePanel, viewport).find((boundary) => (
  boundary.kind === "shared" &&
  boundary.orientation === "horizontal" &&
  boundary.positiveSlotIds.includes(leftEdgeChart.id)
));
assert(leftEdgeChartSupportBoundary, "left-edge layout should keep a chart/support horizontal boundary");
const withLeftEdgeSupportChart = insertPanelAtBoundary(withLeftEdgePanel, leftEdgeChartSupportBoundary.id, "chart", viewport, { symbol: "AAPL" });
const leftEdgeSupportChart = chartSlotBySymbol(withLeftEdgeSupportChart, "AAPL");
assert(Math.round(leftEdgeSupportChart.rect.left) === Math.round(leftEdgeChart.rect.left), "horizontal chart insert should inherit left-edge chart left");
assert(Math.round(rectRight(leftEdgeSupportChart.rect)) === Math.round(rectRight(leftEdgeChart.rect)), "horizontal chart insert should inherit left-edge chart right");
assert(!layoutHasGapsOrOverlaps(withLeftEdgeSupportChart, viewport), "left-edge horizontal chart insertion should preserve exact gutters");
const invalidWideGap = {
  ...withLeftEdgePanel,
  slots: withLeftEdgePanel.slots.map((slot) => slot.id === leftEdgeChart.id
    ? { ...slot, rect: { ...slot.rect, left: slot.rect.left + gutter, width: slot.rect.width - gutter } }
    : slot)
};
assert(layoutHasGapsOrOverlaps(invalidWideGap, viewport), "oversized direct gutter should be invalid");
const afterLeftDelete = removePanelSlot(withLeftEdgePanel, leftEdgeTrade.id, viewport);
assert(afterLeftDelete.slots.length === 4, "deleting left edge panel should remove it");
assert(Math.round(slotByKind(afterLeftDelete, "chart").rect.left) === workspace.left, "chart should flush left again after deleting edge panel");
assert(!layoutHasGapsOrOverlaps(afterLeftDelete, viewport), "left edge deletion should preserve valid layout");

const withLeftEdgeChart = insertPanelAtBoundary(state, chartLeftOuterGuide.id, "chart", viewport, { symbol: "GOOGL" });
assert(withLeftEdgeChart.slots.length === 5, "left page-edge chart insertion should add a chart");
const addedLeftChart = nonDefaultChartSlot(withLeftEdgeChart);
const defaultChartAfterLeftChartInsert = defaultChartSlot(withLeftEdgeChart);
assert(Math.round(addedLeftChart.rect.left) === workspace.left, "inserted left-edge chart should flush to page left");
assert(Math.round(defaultChartAfterLeftChartInsert.rect.left - rectRight(addedLeftChart.rect)) === gutter, "left-edge chart and default chart should keep one gutter");
assert(Math.round(addedLeftChart.rect.top) === workspace.top + gutter, "inserted left-edge chart should use the full workspace top");
assert(Math.round(rectBottom(addedLeftChart.rect)) === rectBottom(workspace) - gutter, "inserted left-edge chart should use the full workspace bottom");
assert(!layoutHasGapsOrOverlaps(withLeftEdgeChart, viewport), "left page-edge chart insertion should preserve valid layout");
const afterLeftChartDelete = removePanelSlot(withLeftEdgeChart, addedLeftChart.id, viewport);
assert(Math.round(defaultChartSlot(afterLeftChartDelete).rect.left) === workspace.left, "default chart should flush left after deleting a left-edge chart");
assert(!layoutHasGapsOrOverlaps(afterLeftChartDelete, viewport), "left edge chart deletion should preserve valid layout");

const withRightEdgePanel = insertPanelAtBoundary(state, chartRightOuterGuide.id, "trade", viewport);
assert(withRightEdgePanel.slots.length === 5, "right page-edge insertion should add a panel");
const rightEdgeTrade = slotByKind(withRightEdgePanel, "trade");
const rightEdgeChart = slotByKind(withRightEdgePanel, "chart");
assert(Math.round(rectRight(workspace) - rectRight(rightEdgeTrade.rect)) === gutter, "right edge panel should keep the page gutter");
assert(Math.round(rightEdgeTrade.rect.left - rectRight(rightEdgeChart.rect)) === gutter, "right edge panel and chart should keep a gutter");
assert(Math.round(rightEdgeTrade.rect.top) === workspace.top + gutter, "right edge panel should use the full workspace top");
assert(Math.round(rectBottom(rightEdgeTrade.rect)) === rectBottom(workspace) - gutter, "right edge panel should use the full workspace bottom");
assert(!layoutHasGapsOrOverlaps(withRightEdgePanel, viewport), "right page-edge insertion should preserve valid layout");
const rightEdgeChartSupportBoundary = detectPanelBoundaries(withRightEdgePanel, viewport).find((boundary) => (
  boundary.kind === "shared" &&
  boundary.orientation === "horizontal" &&
  boundary.positiveSlotIds.includes(rightEdgeChart.id)
));
assert(rightEdgeChartSupportBoundary, "right-edge layout should keep a chart/support horizontal boundary");
const withRightEdgeSupportChart = insertPanelAtBoundary(withRightEdgePanel, rightEdgeChartSupportBoundary.id, "chart", viewport, { symbol: "GOOGL" });
const rightEdgeSupportChart = chartSlotBySymbol(withRightEdgeSupportChart, "GOOGL");
assert(Math.round(rightEdgeSupportChart.rect.left) === Math.round(rightEdgeChart.rect.left), "horizontal chart insert should inherit right-edge chart left");
assert(Math.round(rectRight(rightEdgeSupportChart.rect)) === Math.round(rectRight(rightEdgeChart.rect)), "horizontal chart insert should inherit right-edge chart right");
assert(!layoutHasGapsOrOverlaps(withRightEdgeSupportChart, viewport), "right-edge horizontal chart insertion should preserve exact gutters");

const withRightEdgeChart = insertPanelAtBoundary(state, chartRightOuterGuide.id, "chart", viewport, { symbol: "AAPL" });
assert(withRightEdgeChart.slots.length === 5, "right page-edge chart insertion should add a chart");
const addedRightChart = nonDefaultChartSlot(withRightEdgeChart);
const defaultChartAfterRightChartInsert = defaultChartSlot(withRightEdgeChart);
assert(Math.round(rectRight(addedRightChart.rect)) === rectRight(workspace), "inserted right-edge chart should flush to page right");
assert(Math.round(addedRightChart.rect.left - rectRight(defaultChartAfterRightChartInsert.rect)) === gutter, "default chart and right-edge chart should keep one gutter");
assert(Math.round(addedRightChart.rect.top) === workspace.top + gutter, "inserted right-edge chart should use the full workspace top");
assert(Math.round(rectBottom(addedRightChart.rect)) === rectBottom(workspace) - gutter, "inserted right-edge chart should use the full workspace bottom");
assert(!layoutHasGapsOrOverlaps(withRightEdgeChart, viewport), "right page-edge chart insertion should preserve valid layout");

const withExtraChart = insertPanelAtBoundary(state, chartSupportBoundary.id, "chart", viewport, { symbol: "AAPL" });
assert(withExtraChart.slots.length === 5, "chart insertion should add a second chart");
assert(withExtraChart.slots.filter((slot) => withExtraChart.contents[slot.contentId].kind === "chart").length === 2, "layout should contain two chart contents after chart insertion");
assert(withExtraChart.slots.some((slot) => withExtraChart.contents[slot.contentId].kind === "chart" && withExtraChart.contents[slot.contentId].symbol === "AAPL"), "inserted chart should copy the current symbol");
const insertedSupportChart = chartSlotBySymbol(withExtraChart, "AAPL");
assert(Math.round(insertedSupportChart.rect.left) === workspace.left, "internally inserted chart should inherit the default chart left flush");
assert(Math.round(rectRight(insertedSupportChart.rect)) === rectRight(workspace), "internally inserted chart should inherit the default chart right flush");
const defaultChartAfterExtraChart = defaultChartSlot(withExtraChart);
const extraChartLeftGuide = detectPanelBoundaries(withExtraChart, viewport).find((boundary) => (
  boundary.pageEdge === "left" &&
  boundary.orientation === "vertical" &&
  boundary.positiveSlotIds.includes(defaultChartAfterExtraChart.id)
));
assert(extraChartLeftGuide, "default chart should still expose a precise left page guide when another chart exists");
const withExtraThenLeftPanel = insertPanelAtBoundary(withExtraChart, extraChartLeftGuide.id, "trade", viewport);
const extraLeftPanel = slotByKind(withExtraThenLeftPanel, "trade");
const defaultChartAfterExtraLeftPanel = defaultChartSlot(withExtraThenLeftPanel);
assert(Math.round(defaultChartAfterExtraLeftPanel.rect.left - rectRight(extraLeftPanel.rect)) === gutter, "extra chart state should still insert a left panel with one gutter");
assert(Math.round(extraLeftPanel.rect.top) === workspace.top + gutter, "left inserted panel should use the full workspace top");
assert(Math.round(rectBottom(extraLeftPanel.rect)) === rectBottom(workspace) - gutter, "left inserted panel should use the full workspace bottom");
assert(!layoutHasGapsOrOverlaps(withExtraThenLeftPanel, viewport), "left panel insertion after extra chart should preserve exact gutters");
const withExtraThenLeftChart = insertPanelAtBoundary(withExtraChart, extraChartLeftGuide.id, "chart", viewport, { symbol: "GOOGL" });
const extraLeftChart = chartSlotBySymbol(withExtraThenLeftChart, "GOOGL");
const defaultChartAfterExtraLeftChart = defaultChartSlot(withExtraThenLeftChart);
assert(Math.round(extraLeftChart.rect.left) === workspace.left, "extra-state left chart should flush to page left");
assert(Math.round(defaultChartAfterExtraLeftChart.rect.left - rectRight(extraLeftChart.rect)) === gutter, "extra-state left chart should keep one gutter from the default chart");
assert(Math.round(extraLeftChart.rect.top) === workspace.top + gutter, "extra-state left chart should use the full workspace top");
assert(Math.round(rectBottom(extraLeftChart.rect)) === rectBottom(workspace) - gutter, "extra-state left chart should use the full workspace bottom");
assert(!layoutHasGapsOrOverlaps(withExtraThenLeftChart, viewport), "left chart insertion after extra chart should preserve exact gutters");
const afterExtraLeftChartDelete = removePanelSlot(withExtraThenLeftChart, extraLeftChart.id, viewport);
assert(Math.round(defaultChartSlot(afterExtraLeftChartDelete).rect.left) === workspace.left, "deleting an extra-state left chart should restore default chart flush");
assert(!layoutHasGapsOrOverlaps(afterExtraLeftChartDelete, viewport), "extra-state left chart deletion should preserve exact gutters");
const extraChartRightGuide = detectPanelBoundaries(withExtraChart, viewport).find((boundary) => (
  boundary.pageEdge === "right" &&
  boundary.orientation === "vertical" &&
  boundary.negativeSlotIds.includes(defaultChartAfterExtraChart.id)
));
assert(extraChartRightGuide, "default chart should still expose a precise right page guide when another chart exists");
const withExtraThenRightPanel = insertPanelAtBoundary(withExtraChart, extraChartRightGuide.id, "trade", viewport);
const extraRightPanel = slotByKind(withExtraThenRightPanel, "trade");
const defaultChartAfterExtraRightPanel = defaultChartSlot(withExtraThenRightPanel);
assert(Math.round(extraRightPanel.rect.left - rectRight(defaultChartAfterExtraRightPanel.rect)) === gutter, "extra chart state should still insert a right panel with one gutter");
assert(Math.round(extraRightPanel.rect.top) === workspace.top + gutter, "right inserted panel should use the full workspace top");
assert(Math.round(rectBottom(extraRightPanel.rect)) === rectBottom(workspace) - gutter, "right inserted panel should use the full workspace bottom");
assert(!layoutHasGapsOrOverlaps(withExtraThenRightPanel, viewport), "right panel insertion after extra chart should preserve exact gutters");
const withExtraThenRightChart = insertPanelAtBoundary(withExtraChart, extraChartRightGuide.id, "chart", viewport, { symbol: "TSLA" });
const extraRightChart = chartSlotBySymbol(withExtraThenRightChart, "TSLA");
const defaultChartAfterExtraRightChart = defaultChartSlot(withExtraThenRightChart);
assert(Math.round(rectRight(extraRightChart.rect)) === rectRight(workspace), "extra-state right chart should flush to page right");
assert(Math.round(extraRightChart.rect.left - rectRight(defaultChartAfterExtraRightChart.rect)) === gutter, "extra-state right chart should keep one gutter from the default chart");
assert(Math.round(extraRightChart.rect.top) === workspace.top + gutter, "extra-state right chart should use the full workspace top");
assert(Math.round(rectBottom(extraRightChart.rect)) === rectBottom(workspace) - gutter, "extra-state right chart should use the full workspace bottom");
assert(!layoutHasGapsOrOverlaps(withExtraThenRightChart, viewport), "right chart insertion after extra chart should preserve exact gutters");
const removableChart = withExtraChart.slots.find((slot) => (
  withExtraChart.contents[slot.contentId].kind === "chart" &&
  !withExtraChart.contents[slot.contentId].isDefaultChart
));
assert(removableChart, "inserted chart should be removable and distinct from the default chart");
const supportSlotBeforeChartSwap = withExtraChart.slots.find((slot) => slot.id === newsSlot.id);
assert(supportSlotBeforeChartSwap, "support slot should still exist before chart swap");
const swappedExtraChart = swapPanelContents(withExtraChart, removableChart.id, newsSlot.id);
assert(swappedExtraChart.slots.find((slot) => slot.id === removableChart.id).contentId === newsSlot.contentId, "inserted chart source slot should receive the support content after swap");
assert(swappedExtraChart.contents[swappedExtraChart.slots.find((slot) => slot.id === newsSlot.id).contentId].kind === "chart", "support slot should receive the inserted chart content after swap");
assertDeepEqual(swappedExtraChart.slots.find((slot) => slot.id === newsSlot.id).rect, supportSlotBeforeChartSwap.rect, "chart/support swap should preserve target geometry");
const afterChartDelete = removePanelSlot(withExtraChart, removableChart.id, viewport);
assert(afterChartDelete.slots.filter((slot) => afterChartDelete.contents[slot.contentId].kind === "chart").length === 1, "deleting inserted chart should leave the default chart");
assert(!layoutHasGapsOrOverlaps(afterChartDelete, viewport), "deleting inserted chart should preserve valid layout");

const verticalBoundary = boundaries.find((boundary) => (
  boundary.kind === "shared" &&
  boundary.orientation === "vertical" &&
  boundary.negativeSlotIds.includes(newsSlot.id) &&
  boundary.positiveSlotIds.includes(ontologySlot.id)
));
assert(verticalBoundary, "support row should expose vertical shared guides");
assert(Math.round(verticalBoundary.position) === Math.round(rectRight(newsSlot.rect) + gutter / 2), "vertical guide should sit at gutter center");
const withDuplicateNews = insertPanelAtBoundary(state, verticalBoundary.id, "news", viewport);
assert(withDuplicateNews.slots.filter((slot) => withDuplicateNews.contents[slot.contentId].kind === "news").length === 2, "same panel kind should be insertable as an independent instance");
assert(!layoutHasGapsOrOverlaps(withDuplicateNews, viewport), "duplicate insert should preserve valid gutters");
assert(!insertOptionsForBoundary(state, verticalBoundary.id, viewport).some((option) => option.kind === "chart"), "short support-row vertical guide should not offer chart insertion");

const swapped = swapPanelContents(state, newsSlot.id, ontologySlot.id);
assert(swapped.slots.find((slot) => slot.id === newsSlot.id).contentId === ontologySlot.contentId, "swap should exchange content ids");
assertDeepEqual(swapped.slots.find((slot) => slot.id === newsSlot.id).rect, newsSlot.rect, "swap should keep slot geometry");
assert(!layoutHasGapsOrOverlaps(swapped, viewport), "swapped layout should preserve valid gutters");

const chartDeleteAttempt = removePanelSlot(state, chartSlot.id, viewport);
assertDeepEqual(chartDeleteAttempt, state, "required chart slot should not be removable");
const chartSwapAttempt = swapPanelContents(state, chartSlot.id, newsSlot.id);
assertDeepEqual(chartSwapAttempt, state, "required chart slot should not be swappable");

const snapState = {
  contents: state.contents,
  nextInstance: state.nextInstance,
  slots: [
    {
      ...newsSlot,
      id: "snap-left",
      rect: {
        left: workspace.left + gutter,
        top: workspace.top + gutter,
        width: 150,
        height: 180
      },
      minWidth: 80,
      minHeight: 80
    },
    {
      ...ontologySlot,
      id: "snap-right",
      rect: {
        left: workspace.left + gutter + 150 + gutter,
        top: workspace.top + gutter,
        width: 250,
        height: 180
      },
      minWidth: 80,
      minHeight: 80
    },
    {
      ...companySlot,
      id: "snap-top-fill",
      rect: {
        left: workspace.left + gutter + 150 + gutter + 250 + gutter,
        top: workspace.top + gutter,
        width: rectRight(workspace) - gutter - (workspace.left + gutter + 150 + gutter + 250 + gutter),
        height: 180
      },
      minWidth: 80,
      minHeight: 80
    },
    {
      ...companySlot,
      id: "snap-anchor",
      rect: {
        left: workspace.left + gutter,
        top: workspace.top + gutter + 180 + gutter,
        width: 222,
        height: 104
      },
      minWidth: 80,
      minHeight: 80
    },
    {
      ...ontologySlot,
      id: "snap-bottom-fill",
      rect: {
        left: workspace.left + gutter + 222 + gutter,
        top: workspace.top + gutter + 180 + gutter,
        width: rectRight(workspace) - gutter - (workspace.left + gutter + 222 + gutter),
        height: 104
      },
      minWidth: 80,
      minHeight: 80
    },
    {
      ...chartSlot,
      id: "snap-chart",
      rect: {
        left: workspace.left,
        top: workspace.top + gutter + 180 + gutter + 104 + gutter,
        width: workspace.width,
        height: rectBottom(workspace) - gutter - (workspace.top + gutter + 180 + gutter + 104 + gutter)
      }
    }
  ]
};
assert(!layoutHasGapsOrOverlaps(snapState, viewport), "snap fixture should start as a valid tiled layout");
const snapBoundary = detectPanelBoundaries(snapState, viewport).find((boundary) => (
  boundary.kind === "shared" &&
  boundary.orientation === "vertical" &&
  boundary.negativeSlotIds.includes("snap-left") &&
  boundary.positiveSlotIds.includes("snap-right")
));
assert(snapBoundary, "snap fixture should expose a movable vertical guide");
const snapTarget = rectRight(snapState.slots.find((slot) => slot.id === "snap-anchor").rect) + gutter / 2;
const snapped = resizePanelBoundary(snapState, snapBoundary.id, snapTarget - snapBoundary.position - 4, viewport);
const snappedLeft = snapped.slots.find((slot) => slot.id === "snap-left");
const snappedGuidePosition = rectRight(snappedLeft.rect) + gutter / 2;
assert(
  Math.round(snappedGuidePosition) === Math.round(snapTarget),
  `boundary drag should snap to a nearby panel edge guide: expected ${snapTarget}, got ${snappedGuidePosition}`
);

console.log("Layout grid verified");

function slotByKind(state, kind) {
  const slot = state.slots.find((item) => state.contents[item.contentId].kind === kind);
  assert(slot, `missing slot for kind ${kind}`);
  return slot;
}

function defaultChartSlot(state) {
  const slot = state.slots.find((item) => (
    state.contents[item.contentId].kind === "chart" &&
    state.contents[item.contentId].isDefaultChart
  ));
  assert(slot, "missing default chart slot");
  return slot;
}

function nonDefaultChartSlot(state) {
  const slot = state.slots.find((item) => (
    state.contents[item.contentId].kind === "chart" &&
    !state.contents[item.contentId].isDefaultChart
  ));
  assert(slot, "missing non-default chart slot");
  return slot;
}

function chartSlotBySymbol(state, symbol) {
  const slot = state.slots.find((item) => (
    state.contents[item.contentId].kind === "chart" &&
    state.contents[item.contentId].symbol === symbol
  ));
  assert(slot, `missing chart slot for symbol ${symbol}`);
  return slot;
}

function rectRight(rect) {
  return rect.left + rect.width;
}

function rectBottom(rect) {
  return rect.top + rect.height;
}

function chartMinHeightForTest() {
  return 190;
}

function panelMinHeightForTest() {
  return 104;
}

function loadTsModule(relativePath, mockRequires = {}) {
  const absolutePath = path.join(repoRoot, relativePath);
  const source = fs.readFileSync(absolutePath, "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
      esModuleInterop: true
    }
  }).outputText;
  const module = { exports: {} };
  const dirname = path.dirname(absolutePath);
  const localRequire = (specifier) => {
    if (specifier in mockRequires) {
      return mockRequires[specifier];
    }
    const candidate = path.join(dirname, `${specifier}.ts`);
    if (fs.existsSync(candidate)) {
      return loadTsModule(path.relative(repoRoot, candidate), mockRequires);
    }
    return nodeRequire(specifier);
  };
  vm.runInNewContext(output, {
    exports: module.exports,
    module,
    require: localRequire,
    console
  }, { filename: absolutePath });
  return module.exports;
}

function assertDeepEqual(actual, expected, message) {
  assert(JSON.stringify(actual) === JSON.stringify(expected), `${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}
