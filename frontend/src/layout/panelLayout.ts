import type { CSSProperties } from "react";
import { gridBoundaryX, gridGutter, type GridLineIndex, type PanelGridSpan } from "./grid";
import { workspaceBottomInset, workspaceTopInset } from "./workspaceMetrics";

export type ViewportSize = {
  width: number;
  height: number;
};

export type PanelContentKind = "chart" | "news" | "ontology" | "companyAnalysis" | "trade";

export type PanelSlotId = string;
export type PanelContentId = string;

export type PanelRect = {
  left: number;
  top: number;
  width: number;
  height: number;
};

export type WorkspaceBounds = PanelRect;

export type PanelSlot = {
  id: PanelSlotId;
  contentId: PanelContentId;
  rect: PanelRect;
  minWidth: number;
  minHeight: number;
  required?: boolean;
};

export type PanelContentInstance = {
  id: PanelContentId;
  kind: PanelContentKind;
  title: string;
  instanceIndex: number;
  symbol?: string;
  isDefaultChart?: boolean;
};

export type TiledPanelState = {
  slots: PanelSlot[];
  contents: Record<PanelContentId, PanelContentInstance>;
  nextInstance: number;
};

export type PanelBoundaryOrientation = "vertical" | "horizontal";
export type PanelBoundaryKind = "shared" | "outer";
export type PanelBoundaryInteraction = "resize" | "insert-only";

export type PanelBoundary = {
  id: string;
  kind: PanelBoundaryKind;
  interaction: PanelBoundaryInteraction;
  orientation: PanelBoundaryOrientation;
  position: number;
  rangeStart: number;
  rangeEnd: number;
  negativeSlotIds: PanelSlotId[];
  positiveSlotIds: PanelSlotId[];
  pageEdge?: "left" | "right" | "top" | "bottom";
};

export type BoundaryInsertOption = {
  kind: PanelContentKind;
  title: string;
};

export type InsertPanelOptions = {
  symbol?: string;
};

const panelMinWidth = 180;
const panelMinHeight = 104;
const chartMinHeight = 190;
const defaultInsertWidth = 240;
const defaultInsertHeight = 142;
const boundarySnapTolerance = 10;
const epsilon = 0.5;

export const insertablePanelKinds: PanelContentKind[] = ["news", "ontology", "companyAnalysis", "trade", "chart"];

export function workspaceBounds(
  viewport: ViewportSize,
  topInset = workspaceTopInset,
  bottomInset = workspaceBottomInset
): WorkspaceBounds {
  const top = Math.max(0, Math.round(topInset));
  const bottom = Math.max(top + 1, Math.round(viewport.height - bottomInset));
  return {
    left: 0,
    top,
    width: Math.max(1, Math.round(viewport.width)),
    height: Math.max(1, bottom - top)
  };
}

export function createInitialTiledPanelState(viewport: ViewportSize): TiledPanelState {
  const workspace = workspaceBounds(viewport);
  const gutter = panelGutter(viewport);
  const supportHeight = Math.max(panelMinHeight, Math.round(workspace.height * 0.24));
  const chartTop = workspace.top + supportHeight + gutter * 2;
  const chartHeight = Math.max(chartMinHeight, rectBottom(workspace) - chartTop);
  const supportTop = workspace.top + gutter;
  const supportLeft = workspace.left + gutter;
  const supportWidth = (workspace.width - gutter * 4) / 3;
  const contents: Record<PanelContentId, PanelContentInstance> = {};
  const chart = createPanelContent("chart", 1, { isDefaultChart: true });
  const news = createPanelContent("news", 2);
  const ontology = createPanelContent("ontology", 3);
  const companyAnalysis = createPanelContent("companyAnalysis", 4);
  [chart, news, ontology, companyAnalysis].forEach((content) => {
    contents[content.id] = content;
  });

  return {
    contents,
    nextInstance: 5,
    slots: [
      {
        id: "slot-news",
        contentId: news.id,
        rect: {
          left: supportLeft,
          top: supportTop,
          width: supportWidth,
          height: supportHeight
        },
        minWidth: panelMinWidth,
        minHeight: panelMinHeight
      },
      {
        id: "slot-ontology",
        contentId: ontology.id,
        rect: {
          left: supportLeft + supportWidth + gutter,
          top: supportTop,
          width: supportWidth,
          height: supportHeight
        },
        minWidth: panelMinWidth,
        minHeight: panelMinHeight
      },
      {
        id: "slot-company-analysis",
        contentId: companyAnalysis.id,
        rect: {
          left: supportLeft + (supportWidth + gutter) * 2,
          top: supportTop,
          width: workspace.width - gutter - (supportLeft + (supportWidth + gutter) * 2),
          height: supportHeight
        },
        minWidth: panelMinWidth,
        minHeight: panelMinHeight
      },
      {
        id: "slot-chart",
        contentId: chart.id,
        rect: {
          left: workspace.left,
          top: chartTop,
          width: workspace.width,
          height: chartHeight
        },
        minWidth: panelMinWidth,
        minHeight: chartMinHeight,
        required: true
      }
    ]
  };
}

export function panelContentTitle(kind: PanelContentKind, instanceIndex?: number): string {
  void instanceIndex;
  return {
    chart: "",
    news: "뉴스",
    ontology: "온톨로지",
    companyAnalysis: "기업분석",
    trade: "거래"
  }[kind];
}

export function detectPanelBoundaries(state: TiledPanelState, viewport?: ViewportSize): PanelBoundary[] {
  const inferredViewport = viewport ?? viewportFromState(state);
  const workspace = workspaceBounds(inferredViewport);
  const gutter = panelGutter(inferredViewport);
  const raw: PanelBoundary[] = [];
  for (let leftIndex = 0; leftIndex < state.slots.length; leftIndex += 1) {
    for (let rightIndex = leftIndex + 1; rightIndex < state.slots.length; rightIndex += 1) {
      const a = state.slots[leftIndex];
      const b = state.slots[rightIndex];
      const vertical = sharedVerticalGuide(a, b, gutter);
      if (vertical) {
        raw.push(vertical);
      }
      const horizontal = sharedHorizontalGuide(a, b, gutter);
      if (horizontal) {
        raw.push(horizontal);
      }
    }
  }
  state.slots.forEach((slot) => {
    raw.push(...insertionGuidesForSlot(slot, state, workspace, gutter));
  });
  raw.push(...chartPageEdgeGuides(state, workspace, gutter));
  return removeCoveredPageEdgeGuides(mergeBoundarySegments(raw));
}

export function resizePanelBoundary(
  state: TiledPanelState,
  boundaryId: string,
  delta: number,
  viewport: ViewportSize
): TiledPanelState {
  const boundary = detectPanelBoundaries(state, viewport).find((item) => item.id === boundaryId);
  if (!boundary || boundary.interaction !== "resize") {
    return state;
  }
  const workspace = workspaceBounds(viewport);
  const desiredPosition = snapBoundaryPosition(
    boundary.position + delta,
    boundary,
    state,
    viewport,
    workspace
  );
  const resolvedDelta = clampBoundaryDelta(state, boundary, desiredPosition - boundary.position, viewport);
  if (Math.abs(resolvedDelta) < epsilon) {
    return state;
  }

  const next = {
    ...state,
    slots: state.slots.map((slot) => resizeSlotAtBoundary(slot, boundary, resolvedDelta))
  };
  return layoutHasGapsOrOverlaps(next, viewport) ? state : next;
}

export function canInsertPanelAtBoundary(
  state: TiledPanelState,
  boundaryId: string,
  viewport?: ViewportSize,
  kind?: PanelContentKind
): boolean {
  const inferredViewport = viewport ?? viewportFromState(state);
  const boundary = detectPanelBoundaries(state, inferredViewport).find((item) => item.id === boundaryId);
  if (!boundary) {
    return false;
  }
  const insertSize = minimumInsertSize(boundary.orientation, kind ?? "news");
  const rangeSize = boundary.rangeEnd - boundary.rangeStart;
  const crossMin = minimumCrossSize(boundary.orientation, kind ?? "news");
  const gutter = panelGutter(inferredViewport);
  return rangeSize >= crossMin &&
    boundaryInsertCapacity(state, boundary, kind ?? "news", gutter, workspaceBounds(inferredViewport)) >= insertSize + gutter;
}

export function insertPanelAtBoundary(
  state: TiledPanelState,
  boundaryId: string,
  kind: PanelContentKind,
  viewport?: ViewportSize,
  options: InsertPanelOptions = {}
): TiledPanelState {
  const inferredViewport = viewport ?? viewportFromState(state);
  const boundary = detectPanelBoundaries(state, inferredViewport).find((item) => item.id === boundaryId);
  if (!boundary || !canInsertPanelAtBoundary(state, boundaryId, inferredViewport, kind)) {
    return state;
  }
  const workspace = workspaceBounds(inferredViewport);
  const gutter = panelGutter(inferredViewport);
  const minSize = minimumInsertSize(boundary.orientation, kind);
  const defaultSize = defaultInsertSize(boundary.orientation, kind);
  const insertSize = Math.max(minSize, Math.min(defaultSize, boundaryInsertCapacity(state, boundary, kind, gutter, workspace) - gutter));
  const shrinkTotal = insertSize + gutter;
  const { negative, positive } = distributeShrink(
    shrinkTotal,
    sideShrinkCapacity(state, boundary, "negative"),
    sideShrinkCapacity(state, boundary, "positive")
  );
  const content = createPanelContent(kind, state.nextInstance, {
    symbol: kind === "chart" ? options.symbol : undefined
  });
  const slot: PanelSlot = {
    id: `slot-${kind}-${state.nextInstance}`,
    contentId: content.id,
    rect: insertedPanelRect(state, boundary, negative, insertSize, gutter, kind, workspace),
    minWidth: panelMinWidth,
    minHeight: kind === "chart" ? chartMinHeight : panelMinHeight
  };
  const next = {
    ...state,
    contents: {
      ...state.contents,
      [content.id]: content
    },
    nextInstance: state.nextInstance + 1,
    slots: [
      ...state.slots.map((item) => shrinkSlotForInsert(state, item, boundary, negative, positive, gutter, kind, workspace)),
      slot
    ]
  };
  return layoutHasGapsOrOverlaps(next, inferredViewport) ? state : next;
}

export function removePanelSlot(state: TiledPanelState, slotId: PanelSlotId, viewport?: ViewportSize): TiledPanelState {
  const removed = state.slots.find((slot) => slot.id === slotId);
  if (!removed || state.contents[removed.contentId]?.isDefaultChart) {
    return state;
  }
  const nextContents = { ...state.contents };
  delete nextContents[removed.contentId];
  const inferredViewport = viewport ?? viewportFromState(state);
  const expanded = expandAdjacentSlotsAfterRemoval(
    {
      ...state,
      contents: nextContents,
      slots: state.slots.filter((slot) => slot.id !== removed.id)
    },
    removed,
    inferredViewport
  );
  if (expanded && !layoutHasGapsOrOverlaps(expanded, inferredViewport)) {
    return expanded;
  }
  const neighbor = removableNeighbor(state, removed, panelGutter(inferredViewport));
  if (!neighbor) {
    return state;
  }
  const fallback = {
    ...state,
    contents: nextContents,
    slots: state.slots
      .filter((slot) => slot.id !== removed.id)
      .map((slot) => slot.id === neighbor.slot.id ? { ...slot, rect: unionRect(slot.rect, removed.rect) } : slot)
  };
  return layoutHasGapsOrOverlaps(fallback, inferredViewport) ? state : fallback;
}

export function setPanelContentSymbol(
  state: TiledPanelState,
  contentId: PanelContentId,
  symbol: string
): TiledPanelState {
  const content = state.contents[contentId];
  if (!content || content.kind !== "chart" || content.isDefaultChart) {
    return state;
  }
  return {
    ...state,
    contents: {
      ...state.contents,
      [contentId]: {
        ...content,
        symbol: symbol.toUpperCase()
      }
    }
  };
}

export function swapPanelContents(
  state: TiledPanelState,
  sourceSlotId: PanelSlotId,
  targetSlotId: PanelSlotId
): TiledPanelState {
  if (sourceSlotId === targetSlotId) {
    return state;
  }
  const source = state.slots.find((slot) => slot.id === sourceSlotId);
  const target = state.slots.find((slot) => slot.id === targetSlotId);
  if (!source || !target || source.required || target.required) {
    return state;
  }
  return {
    ...state,
    slots: state.slots.map((slot) => {
      if (slot.id === source.id) {
        return { ...slot, contentId: target.contentId };
      }
      if (slot.id === target.id) {
        return { ...slot, contentId: source.contentId };
      }
      return slot;
    })
  };
}

export function scaleTiledPanelState(
  state: TiledPanelState,
  previousViewport: ViewportSize,
  nextViewport: ViewportSize
): TiledPanelState {
  const previous = workspaceBounds(previousViewport);
  const next = workspaceBounds(nextViewport);
  const scaleX = next.width / Math.max(1, previous.width);
  const scaleY = next.height / Math.max(1, previous.height);
  return {
    ...state,
    slots: state.slots.map((slot) => ({
      ...slot,
      rect: {
        left: next.left + (slot.rect.left - previous.left) * scaleX,
        top: next.top + (slot.rect.top - previous.top) * scaleY,
        width: slot.rect.width * scaleX,
        height: slot.rect.height * scaleY
      }
    }))
  };
}

export function layoutHasGapsOrOverlaps(
  state: TiledPanelState,
  viewport: ViewportSize,
  tolerance = 1
): boolean {
  const workspace = workspaceBounds(viewport);
  const gutter = panelGutter(viewport);
  for (const slot of state.slots) {
    const isChart = isChartSlot(state, slot);
    const slotRight = rectRight(slot.rect);
    const slotBottom = rectBottom(slot.rect);
    const leftLimit = isChart && almostEqual(slot.rect.left, workspace.left) ? workspace.left : workspace.left + gutter;
    const rightLimit = isChart && almostEqual(slotRight, rectRight(workspace)) ? rectRight(workspace) : rectRight(workspace) - gutter;
    const bottomLimit = isChart && almostEqual(slotBottom, rectBottom(workspace)) ? rectBottom(workspace) : rectBottom(workspace) - gutter;
    if (
      slot.rect.left < leftLimit - tolerance ||
      slot.rect.top < workspace.top + gutter - tolerance ||
      slotRight > rightLimit + tolerance ||
      slotBottom > bottomLimit + tolerance ||
      slot.rect.width < effectiveSlotMinWidth(state, slot) - tolerance ||
      slot.rect.height < effectiveSlotMinHeight(state, slot) - tolerance
    ) {
      return true;
    }
  }

  for (let leftIndex = 0; leftIndex < state.slots.length; leftIndex += 1) {
    for (let rightIndex = leftIndex + 1; rightIndex < state.slots.length; rightIndex += 1) {
      const leftSlot = state.slots[leftIndex];
      const rightSlot = state.slots[rightIndex];
      const a = leftSlot.rect;
      const b = rightSlot.rect;
      if (rectsOverlap(a, b, tolerance)) {
        return true;
      }
      if (rangesOverlap(a.top, rectBottom(a), b.top, rectBottom(b), tolerance)) {
        const first = a.left < b.left ? leftSlot : rightSlot;
        const second = a.left < b.left ? rightSlot : leftSlot;
        const gap = second.rect.left - rectRight(first.rect);
        const overlap = {
          start: Math.max(first.rect.top, second.rect.top),
          end: Math.min(rectBottom(first.rect), rectBottom(second.rect))
        };
        if (
          gap > tolerance &&
          !hasHorizontalSlotBetween(state, first, second, overlap, tolerance) &&
          Math.abs(gap - gutter) > tolerance
        ) {
          return true;
        }
      }
      if (rangesOverlap(a.left, rectRight(a), b.left, rectRight(b), tolerance)) {
        const first = a.top < b.top ? leftSlot : rightSlot;
        const second = a.top < b.top ? rightSlot : leftSlot;
        const gap = second.rect.top - rectBottom(first.rect);
        const overlap = {
          start: Math.max(first.rect.left, second.rect.left),
          end: Math.min(rectRight(first.rect), rectRight(second.rect))
        };
        if (
          gap > tolerance &&
          !hasVerticalSlotBetween(state, first, second, overlap, tolerance) &&
          Math.abs(gap - gutter) > tolerance
        ) {
          return true;
        }
      }
    }
  }
  return false;
}

export function panelSlotStyle(slot: PanelSlot): CSSProperties {
  return {
    left: slot.rect.left,
    top: slot.rect.top,
    width: slot.rect.width,
    height: slot.rect.height
  };
}

export function insertOptionsForBoundary(state: TiledPanelState, boundaryId: string, viewport?: ViewportSize): BoundaryInsertOption[] {
  return insertablePanelKinds.filter((kind) => canInsertPanelAtBoundary(state, boundaryId, viewport, kind)).map((kind) => ({
    kind,
    title: boundaryInsertTitle(kind)
  }));
}

export function chartGridSafePositions(viewport: ViewportSize): number[] {
  const gutter = gridGutter(viewport.width);
  return ([0, 1, 2, 3, 4] as GridLineIndex[]).map((line) => {
    if (line === 0) {
      return 0;
    }
    if (line === 4) {
      return viewport.width;
    }
    return gridBoundaryX(line, viewport.width) + gutter;
  });
}

export function chartGridSpanFromRect(rect: PanelRect, viewport: ViewportSize): PanelGridSpan {
  const start = nearestGridLine(rect.left, viewport);
  const end = nearestGridLine(rectRight(rect), viewport);
  return start < end ? { start, end } : { start, end: Math.min(4, start + 1) as GridLineIndex };
}

export function panelGutter(viewport: ViewportSize): number {
  return gridGutter(viewport.width);
}

function createPanelContent(
  kind: PanelContentKind,
  instanceIndex: number,
  options: Pick<PanelContentInstance, "symbol" | "isDefaultChart"> = {}
): PanelContentInstance {
  return {
    id: `content-${kind}-${instanceIndex}`,
    kind,
    title: panelContentTitle(kind, instanceIndex),
    instanceIndex,
    ...options
  };
}

function boundaryInsertTitle(kind: PanelContentKind): string {
  return kind === "chart" ? "차트" : panelContentTitle(kind);
}

function isChartSlot(state: TiledPanelState, slot: PanelSlot): boolean {
  return state.contents[slot.contentId]?.kind === "chart";
}

function effectiveSlotMinWidth(_state: TiledPanelState, slot: PanelSlot): number {
  return slot.minWidth;
}

function effectiveSlotMinHeight(_state: TiledPanelState, slot: PanelSlot): number {
  return slot.minHeight;
}

function minimumInsertSize(orientation: PanelBoundaryOrientation, kind: PanelContentKind): number {
  if (kind === "chart" && orientation === "horizontal") {
    return chartMinHeight;
  }
  return orientation === "vertical" ? panelMinWidth : panelMinHeight;
}

function defaultInsertSize(orientation: PanelBoundaryOrientation, kind: PanelContentKind): number {
  if (kind === "chart" && orientation === "horizontal") {
    return Math.max(chartMinHeight, defaultInsertHeight);
  }
  return orientation === "vertical" ? defaultInsertWidth : defaultInsertHeight;
}

function minimumCrossSize(orientation: PanelBoundaryOrientation, kind: PanelContentKind): number {
  if (orientation === "vertical") {
    return kind === "chart" ? chartMinHeight : panelMinHeight;
  }
  return panelMinWidth;
}

function sharedVerticalGuide(a: PanelSlot, b: PanelSlot, gutter: number): PanelBoundary | null {
  const aRight = rectRight(a.rect);
  const bRight = rectRight(b.rect);
  if (almostEqual(b.rect.left - aRight, gutter)) {
    return boundaryFromOverlap("shared", "vertical", aRight + gutter / 2, a, b, verticalOverlap(a, b));
  }
  if (almostEqual(a.rect.left - bRight, gutter)) {
    return boundaryFromOverlap("shared", "vertical", bRight + gutter / 2, b, a, verticalOverlap(a, b));
  }
  return null;
}

function sharedHorizontalGuide(a: PanelSlot, b: PanelSlot, gutter: number): PanelBoundary | null {
  const aBottom = rectBottom(a.rect);
  const bBottom = rectBottom(b.rect);
  if (almostEqual(b.rect.top - aBottom, gutter)) {
    return boundaryFromOverlap("shared", "horizontal", aBottom + gutter / 2, a, b, horizontalOverlap(a, b));
  }
  if (almostEqual(a.rect.top - bBottom, gutter)) {
    return boundaryFromOverlap("shared", "horizontal", bBottom + gutter / 2, b, a, horizontalOverlap(a, b));
  }
  return null;
}

function insertionGuidesForSlot(slot: PanelSlot, state: TiledPanelState, workspace: WorkspaceBounds, gutter: number): PanelBoundary[] {
  const guides: PanelBoundary[] = [];
  const slotRight = rectRight(slot.rect);
  const slotBottom = rectBottom(slot.rect);
  if (!hasSharedGuideOnSide(slot, state, "left", gutter) && slot.rect.left > workspace.left + epsilon) {
    guides.push(withBoundaryId({
      id: "",
      kind: "outer",
      interaction: "insert-only",
      orientation: "vertical",
      position: slot.rect.left - gutter / 2,
      rangeStart: slot.rect.top,
      rangeEnd: slotBottom,
      negativeSlotIds: [],
      positiveSlotIds: [slot.id]
    }));
  }
  if (!hasSharedGuideOnSide(slot, state, "right", gutter) && slotRight < rectRight(workspace) - epsilon) {
    guides.push(withBoundaryId({
      id: "",
      kind: "outer",
      interaction: "insert-only",
      orientation: "vertical",
      position: slotRight + gutter / 2,
      rangeStart: slot.rect.top,
      rangeEnd: slotBottom,
      negativeSlotIds: [slot.id],
      positiveSlotIds: []
    }));
  }
  if (!hasSharedGuideOnSide(slot, state, "top", gutter) && slot.rect.top > workspace.top + epsilon) {
    guides.push(withBoundaryId({
      id: "",
      kind: "outer",
      interaction: "insert-only",
      orientation: "horizontal",
      position: slot.rect.top - gutter / 2,
      rangeStart: slot.rect.left,
      rangeEnd: slotRight,
      negativeSlotIds: [],
      positiveSlotIds: [slot.id]
    }));
  }
  if (!hasSharedGuideOnSide(slot, state, "bottom", gutter) && slotBottom < rectBottom(workspace) - epsilon) {
    guides.push(withBoundaryId({
      id: "",
      kind: "outer",
      interaction: "insert-only",
      orientation: "horizontal",
      position: slotBottom + gutter / 2,
      rangeStart: slot.rect.left,
      rangeEnd: slotRight,
      negativeSlotIds: [slot.id],
      positiveSlotIds: []
    }));
  }
  return guides;
}

function chartPageEdgeGuides(state: TiledPanelState, workspace: WorkspaceBounds, gutter: number): PanelBoundary[] {
  const guides: PanelBoundary[] = [];
  const workspaceRight = rectRight(workspace);
  const workspaceBottom = rectBottom(workspace);
  const chartSlots = state.slots.filter((slot) => isChartSlot(state, slot));
  chartSlots.forEach((slot) => {
    const slotBottom = rectBottom(slot.rect);
    if (almostEqual(slot.rect.left, workspace.left)) {
      guides.push(withBoundaryId({
        id: "",
        kind: "outer",
        interaction: "insert-only",
        orientation: "vertical",
        position: workspace.left + gutter / 2,
        rangeStart: workspace.top + gutter,
        rangeEnd: workspaceBottom - gutter,
        negativeSlotIds: [],
        positiveSlotIds: fullHeightPageSideSlotIds(state, workspace, gutter, "left"),
        pageEdge: "left"
      }));
    }
    if (almostEqual(rectRight(slot.rect), workspaceRight)) {
      guides.push(withBoundaryId({
        id: "",
        kind: "outer",
        interaction: "insert-only",
        orientation: "vertical",
        position: workspaceRight - gutter / 2,
        rangeStart: workspace.top + gutter,
        rangeEnd: workspaceBottom - gutter,
        negativeSlotIds: fullHeightPageSideSlotIds(state, workspace, gutter, "right"),
        positiveSlotIds: [],
        pageEdge: "right"
      }));
    }
    if (almostEqual(slotBottom, workspaceBottom)) {
      guides.push(withBoundaryId({
        id: "",
        kind: "outer",
        interaction: "insert-only",
        orientation: "horizontal",
        position: workspaceBottom - gutter / 2,
        rangeStart: slot.rect.left,
        rangeEnd: rectRight(slot.rect),
        negativeSlotIds: [slot.id],
        positiveSlotIds: [],
        pageEdge: "bottom"
      }));
    }
  });
  return guides.filter((guide) => guide.negativeSlotIds.length + guide.positiveSlotIds.length > 0);
}

function fullHeightPageSideSlotIds(
  state: TiledPanelState,
  workspace: WorkspaceBounds,
  gutter: number,
  side: "left" | "right"
): PanelSlotId[] {
  const workspaceRight = rectRight(workspace);
  return state.slots
    .filter((slot) => (
      side === "left"
        ? slot.rect.left <= workspace.left + gutter + epsilon
        : rectRight(slot.rect) >= workspaceRight - gutter - epsilon
    ))
    .map((slot) => slot.id);
}

function hasSharedGuideOnSide(slot: PanelSlot, state: TiledPanelState, side: "left" | "right" | "top" | "bottom", gutter: number): boolean {
  return state.slots.some((other) => {
    if (other.id === slot.id) {
      return false;
    }
    if (side === "left") {
      return almostEqual(slot.rect.left - rectRight(other.rect), gutter) && verticalOverlapSlots(slot, other) > epsilon;
    }
    if (side === "right") {
      return almostEqual(other.rect.left - rectRight(slot.rect), gutter) && verticalOverlapSlots(slot, other) > epsilon;
    }
    if (side === "top") {
      return almostEqual(slot.rect.top - rectBottom(other.rect), gutter) && horizontalOverlapSlots(slot, other) > epsilon;
    }
    return almostEqual(other.rect.top - rectBottom(slot.rect), gutter) && horizontalOverlapSlots(slot, other) > epsilon;
  });
}

function boundaryFromOverlap(
  kind: PanelBoundaryKind,
  orientation: PanelBoundaryOrientation,
  position: number,
  negative: PanelSlot,
  positive: PanelSlot,
  overlap: { start: number; end: number }
): PanelBoundary | null {
  if (overlap.end - overlap.start <= epsilon) {
    return null;
  }
  return withBoundaryId({
    id: "",
    kind,
    interaction: "resize",
    orientation,
    position,
    rangeStart: overlap.start,
    rangeEnd: overlap.end,
    negativeSlotIds: [negative.id],
    positiveSlotIds: [positive.id]
  });
}

function mergeBoundarySegments(boundaries: PanelBoundary[]): PanelBoundary[] {
  const sorted = [...boundaries].sort((a, b) => (
    a.orientation.localeCompare(b.orientation) ||
    a.position - b.position ||
    a.rangeStart - b.rangeStart ||
    a.rangeEnd - b.rangeEnd
  ));
  const merged: PanelBoundary[] = [];
  for (const boundary of sorted) {
    const last = merged[merged.length - 1];
    if (
      last &&
      last.kind === boundary.kind &&
      last.interaction === boundary.interaction &&
      last.pageEdge === boundary.pageEdge &&
      last.orientation === boundary.orientation &&
      almostEqual(last.position, boundary.position) &&
      sameSideSignature(last) === sameSideSignature(boundary)
    ) {
      last.rangeStart = Math.min(last.rangeStart, boundary.rangeStart);
      last.rangeEnd = Math.max(last.rangeEnd, boundary.rangeEnd);
      last.negativeSlotIds = uniqueStrings([...last.negativeSlotIds, ...boundary.negativeSlotIds]);
      last.positiveSlotIds = uniqueStrings([...last.positiveSlotIds, ...boundary.positiveSlotIds]);
      merged[merged.length - 1] = withBoundaryId(last);
    } else {
      merged.push(withBoundaryId({ ...boundary }));
    }
  }
  return merged;
}

function removeCoveredPageEdgeGuides(boundaries: PanelBoundary[]): PanelBoundary[] {
  return boundaries.filter((boundary) => {
    if (boundary.pageEdge) {
      return true;
    }
    return !boundaries.some((edgeBoundary) => (
      Boolean(edgeBoundary.pageEdge) &&
      edgeBoundary.orientation === boundary.orientation &&
      almostEqual(edgeBoundary.position, boundary.position) &&
      edgeBoundary.rangeStart <= boundary.rangeStart + epsilon &&
      edgeBoundary.rangeEnd >= boundary.rangeEnd - epsilon
    ));
  });
}

function withBoundaryId(boundary: PanelBoundary): PanelBoundary {
  return {
    ...boundary,
    id: [
      boundary.kind,
      boundary.interaction,
      boundary.orientation,
      Math.round(boundary.position),
      Math.round(boundary.rangeStart),
      Math.round(boundary.rangeEnd),
      boundary.negativeSlotIds.join("."),
      boundary.positiveSlotIds.join("."),
      boundary.pageEdge ?? ""
    ].join(":")
  };
}

function sameSideSignature(boundary: PanelBoundary): string {
  return `${boundary.negativeSlotIds.length > 0 ? "n" : ""}/${boundary.positiveSlotIds.length > 0 ? "p" : ""}`;
}

function clampBoundaryDelta(state: TiledPanelState, boundary: PanelBoundary, delta: number, viewport: ViewportSize): number {
  let minDelta = Number.NEGATIVE_INFINITY;
  let maxDelta = Number.POSITIVE_INFINITY;
  const workspace = workspaceBounds(viewport);
  const gutter = panelGutter(viewport);
  const negativeSlots = boundary.negativeSlotIds.map((id) => requiredSlot(state, id));
  const positiveSlots = boundary.positiveSlotIds.map((id) => requiredSlot(state, id));
  if (boundary.orientation === "vertical") {
    negativeSlots.forEach((slot) => {
      minDelta = Math.max(minDelta, effectiveSlotMinWidth(state, slot) - slot.rect.width);
      const rightLimit = isChartSlot(state, slot) ? rectRight(workspace) : rectRight(workspace) - gutter;
      maxDelta = Math.min(maxDelta, rightLimit - rectRight(slot.rect));
    });
    positiveSlots.forEach((slot) => {
      const leftLimit = isChartSlot(state, slot) ? workspace.left : workspace.left + gutter;
      minDelta = Math.max(minDelta, leftLimit - slot.rect.left);
      maxDelta = Math.min(maxDelta, slot.rect.width - effectiveSlotMinWidth(state, slot));
    });
  } else {
    negativeSlots.forEach((slot) => {
      minDelta = Math.max(minDelta, effectiveSlotMinHeight(state, slot) - slot.rect.height);
      const bottomLimit = rectBottom(workspace) - gutter;
      maxDelta = Math.min(maxDelta, bottomLimit - rectBottom(slot.rect));
    });
    positiveSlots.forEach((slot) => {
      const topLimit = workspace.top + gutter;
      minDelta = Math.max(minDelta, topLimit - slot.rect.top);
      maxDelta = Math.min(maxDelta, slot.rect.height - effectiveSlotMinHeight(state, slot));
    });
  }
  return clamp(delta, minDelta, maxDelta);
}

function resizeSlotAtBoundary(slot: PanelSlot, boundary: PanelBoundary, delta: number): PanelSlot {
  if (boundary.negativeSlotIds.includes(slot.id)) {
    return boundary.orientation === "vertical"
      ? { ...slot, rect: { ...slot.rect, width: slot.rect.width + delta } }
      : { ...slot, rect: { ...slot.rect, height: slot.rect.height + delta } };
  }
  if (boundary.positiveSlotIds.includes(slot.id)) {
    return boundary.orientation === "vertical"
      ? { ...slot, rect: { ...slot.rect, left: slot.rect.left + delta, width: slot.rect.width - delta } }
      : { ...slot, rect: { ...slot.rect, top: slot.rect.top + delta, height: slot.rect.height - delta } };
  }
  return slot;
}

function snapBoundaryPosition(
  desiredPosition: number,
  boundary: PanelBoundary,
  state: TiledPanelState,
  viewport: ViewportSize,
  workspace: WorkspaceBounds
): number {
  const affected = new Set([...boundary.negativeSlotIds, ...boundary.positiveSlotIds]);
  const gutter = panelGutter(viewport);
  const axisPositions = boundary.orientation === "vertical"
    ? state.slots.flatMap((slot) => [slot.rect.left - gutter / 2, rectRight(slot.rect) + gutter / 2])
    : state.slots.flatMap((slot) => [slot.rect.top - gutter / 2, rectBottom(slot.rect) + gutter / 2]);
  const chartAffected = state.slots.some((slot) => affected.has(slot.id) && isChartSlot(state, slot));
  const candidates = [
    boundary.orientation === "vertical" ? workspace.left + gutter / 2 : workspace.top + gutter / 2,
    boundary.orientation === "vertical" ? rectRight(workspace) - gutter / 2 : rectBottom(workspace) - gutter / 2,
    ...axisPositions,
    ...(boundary.orientation === "vertical" && chartAffected ? chartGridSafePositions(viewport) : [])
  ];
  const distinct = sortedUnique(candidates);
  const found = distinct.find((candidate) => (
    Math.abs(candidate - boundary.position) > epsilon &&
    Math.abs(candidate - desiredPosition) <= boundarySnapTolerance
  ));
  return found ?? desiredPosition;
}

function boundaryShrinkCapacity(state: TiledPanelState, boundary: PanelBoundary): number {
  return sideShrinkCapacity(state, boundary, "negative") + sideShrinkCapacity(state, boundary, "positive");
}

function boundaryInsertCapacity(
  state: TiledPanelState,
  boundary: PanelBoundary,
  insertedKind: PanelContentKind,
  gutter: number,
  workspace: WorkspaceBounds
): number {
  return sideInsertCapacity(state, boundary, "negative", insertedKind, gutter, workspace) +
    sideInsertCapacity(state, boundary, "positive", insertedKind, gutter, workspace);
}

function sideShrinkCapacity(
  state: TiledPanelState,
  boundary: PanelBoundary,
  side: "negative" | "positive"
): number {
  const ids = side === "negative" ? boundary.negativeSlotIds : boundary.positiveSlotIds;
  const capacities = ids.map((id) => {
    const slot = requiredSlot(state, id);
    return boundary.orientation === "vertical"
      ? Math.max(0, slot.rect.width - effectiveSlotMinWidth(state, slot))
      : Math.max(0, slot.rect.height - effectiveSlotMinHeight(state, slot));
  });
  return capacities.length ? Math.min(...capacities) : 0;
}

function sideInsertCapacity(
  state: TiledPanelState,
  boundary: PanelBoundary,
  side: "negative" | "positive",
  insertedKind: PanelContentKind,
  gutter: number,
  workspace: WorkspaceBounds
): number {
  const ids = side === "negative" ? boundary.negativeSlotIds : boundary.positiveSlotIds;
  const capacities = ids.map((id) => {
    const slot = requiredSlot(state, id);
    const capacity = boundary.orientation === "vertical"
      ? Math.max(0, slot.rect.width - effectiveSlotMinWidth(state, slot))
      : Math.max(0, slot.rect.height - effectiveSlotMinHeight(state, slot));
    return Math.max(0, capacity - pageEdgeExtraShrinkForInsertSlot(state, slot, boundary, insertedKind, gutter, workspace));
  });
  return capacities.length ? Math.min(...capacities) : 0;
}

function distributeShrink(total: number, negativeCapacity: number, positiveCapacity: number): { negative: number; positive: number } {
  if (negativeCapacity <= 0) {
    return { negative: 0, positive: Math.min(total, positiveCapacity) };
  }
  if (positiveCapacity <= 0) {
    return { negative: Math.min(total, negativeCapacity), positive: 0 };
  }
  let negative = Math.min(total / 2, negativeCapacity);
  let positive = total - negative;
  if (positive > positiveCapacity) {
    positive = positiveCapacity;
    negative = total - positive;
  }
  return { negative, positive };
}

function shrinkSlotForInsert(
  state: TiledPanelState,
  slot: PanelSlot,
  boundary: PanelBoundary,
  negativeShrink: number,
  positiveShrink: number,
  gutter: number,
  insertedKind: PanelContentKind,
  workspace: WorkspaceBounds
): PanelSlot {
  if (boundary.negativeSlotIds.includes(slot.id)) {
    const shrink = boundary.pageEdge === "right"
      ? pageEdgeShrinkForSlot(state, slot, insertedKind, negativeShrink, gutter, "right", workspace)
      : boundary.pageEdge === "bottom"
        ? pageEdgeShrinkForSlot(state, slot, insertedKind, negativeShrink, gutter, "bottom", workspace)
        : negativeShrink;
    return boundary.orientation === "vertical"
      ? { ...slot, rect: { ...slot.rect, width: slot.rect.width - shrink } }
      : { ...slot, rect: { ...slot.rect, height: slot.rect.height - shrink } };
  }
  if (boundary.positiveSlotIds.includes(slot.id)) {
    const shrink = boundary.pageEdge === "left"
      ? pageEdgeShrinkForSlot(state, slot, insertedKind, positiveShrink, gutter, "left", workspace)
      : positiveShrink;
    return boundary.orientation === "vertical"
      ? { ...slot, rect: { ...slot.rect, left: slot.rect.left + shrink, width: slot.rect.width - shrink } }
      : { ...slot, rect: { ...slot.rect, top: slot.rect.top + shrink, height: slot.rect.height - shrink } };
  }
  return slot;
}

function pageEdgeShrinkForSlot(
  state: TiledPanelState,
  slot: PanelSlot,
  insertedKind: PanelContentKind,
  shrink: number,
  gutter: number,
  side: "left" | "right" | "bottom",
  workspace: WorkspaceBounds
): number {
  const isFlushSlot = slotFlushesPageEdge(slot, side, workspace);
  if (insertedKind !== "chart" && isChartSlot(state, slot) && isFlushSlot) {
    return shrink + gutter;
  }
  if (insertedKind === "chart" && !isFlushSlot) {
    return Math.max(0, shrink - gutter);
  }
  return shrink;
}

function pageEdgeExtraShrinkForInsertSlot(
  state: TiledPanelState,
  slot: PanelSlot,
  boundary: PanelBoundary,
  insertedKind: PanelContentKind,
  gutter: number,
  workspace: WorkspaceBounds
): number {
  if (
    insertedKind === "chart" ||
    !boundary.pageEdge ||
    !isChartSlot(state, slot) ||
    !slotFlushesPageEdge(slot, boundary.pageEdge, workspace)
  ) {
    return 0;
  }
  return gutter;
}

function slotFlushesPageEdge(slot: PanelSlot, side: NonNullable<PanelBoundary["pageEdge"]>, workspace: WorkspaceBounds): boolean {
  if (side === "left") {
    return almostEqual(slot.rect.left, workspace.left);
  }
  if (side === "right") {
    return almostEqual(rectRight(slot.rect), rectRight(workspace));
  }
  if (side === "bottom") {
    return almostEqual(rectBottom(slot.rect), rectBottom(workspace));
  }
  return almostEqual(slot.rect.top, workspace.top);
}

function insertedPanelRect(
  state: TiledPanelState,
  boundary: PanelBoundary,
  negativeShrink: number,
  insertSize: number,
  gutter: number,
  kind: PanelContentKind,
  workspace: WorkspaceBounds
): PanelRect {
  if (boundary.orientation === "vertical") {
    const left = kind === "chart" && boundary.pageEdge === "left"
      ? boundary.position - gutter / 2
      : kind === "chart" && boundary.pageEdge === "right"
        ? boundary.position + gutter / 2 - insertSize
        : boundary.negativeSlotIds.length ? boundary.position + gutter / 2 - negativeShrink : boundary.position + gutter / 2;
    return {
      left,
      top: boundary.rangeStart,
      width: insertSize,
      height: boundary.rangeEnd - boundary.rangeStart
    };
  }
  const top = kind === "chart" && boundary.pageEdge === "bottom"
    ? boundary.position + gutter / 2 - insertSize
    : boundary.negativeSlotIds.length ? boundary.position + gutter / 2 - negativeShrink : boundary.position + gutter / 2;
  const inheritedChartBounds = kind === "chart" ? horizontalChartInsertBounds(state, boundary) : null;
  if (inheritedChartBounds) {
    return {
      left: inheritedChartBounds.left,
      top,
      width: inheritedChartBounds.right - inheritedChartBounds.left,
      height: insertSize
    };
  }
  const workspaceRight = rectRight(workspace);
  const insetLeft = kind !== "chart" && boundary.rangeStart <= workspace.left + epsilon ? gutter : 0;
  const insetRight = kind !== "chart" && boundary.rangeEnd >= workspaceRight - epsilon ? gutter : 0;
  return {
    left: boundary.rangeStart + insetLeft,
    top,
    width: boundary.rangeEnd - boundary.rangeStart - insetLeft - insetRight,
    height: insertSize
  };
}

function horizontalChartInsertBounds(
  state: TiledPanelState,
  boundary: PanelBoundary
): { left: number; right: number } | null {
  if (boundary.orientation !== "horizontal") {
    return null;
  }
  const chartSlot = chartSlotFromIds(state, boundary.positiveSlotIds) ?? chartSlotFromIds(state, boundary.negativeSlotIds);
  return chartSlot ? { left: chartSlot.rect.left, right: rectRight(chartSlot.rect) } : null;
}

function chartSlotFromIds(state: TiledPanelState, slotIds: PanelSlotId[]): PanelSlot | null {
  for (const slotId of slotIds) {
    const slot = state.slots.find((item) => item.id === slotId);
    if (slot && isChartSlot(state, slot)) {
      return slot;
    }
  }
  return null;
}

function expandAdjacentSlotsAfterRemoval(
  stateWithoutRemoved: TiledPanelState,
  removed: PanelSlot,
  viewport: ViewportSize
): TiledPanelState | null {
  const gutter = panelGutter(viewport);
  const workspace = workspaceBounds(viewport);
  const candidates: TiledPanelState[] = [];
  (["right", "left", "bottom", "top"] as const).forEach((side) => {
    const adjusted = expandAdjacentSlotsOnSide(stateWithoutRemoved, removed, side, workspace, gutter);
    if (adjusted) {
      candidates.push(adjusted);
    }
  });
  return candidates.find((candidate) => !layoutHasGapsOrOverlaps(candidate, viewport)) ?? null;
}

function expandAdjacentSlotsOnSide(
  state: TiledPanelState,
  removed: PanelSlot,
  side: "right" | "left" | "bottom" | "top",
  workspace: WorkspaceBounds,
  gutter: number
): TiledPanelState | null {
  const affected = state.slots.filter((slot) => adjacentToRemoved(slot, removed, side, gutter));
  if (!affected.length) {
    return null;
  }
  const removedTouchesLeft = removed.rect.left <= workspace.left + gutter + epsilon;
  const removedTouchesRight = rectRight(removed.rect) >= rectRight(workspace) - gutter - epsilon;
  const removedTouchesTop = removed.rect.top <= workspace.top + gutter + epsilon;
  const removedTouchesBottom = rectBottom(removed.rect) >= rectBottom(workspace) - gutter - epsilon;
  const workspaceRight = rectRight(workspace);
  const workspaceBottom = rectBottom(workspace);
  return {
    ...state,
    slots: state.slots.map((slot) => {
      if (!affected.some((item) => item.id === slot.id)) {
        return slot;
      }
      if (side === "right") {
        const desiredLeft = isChartSlot(state, slot) ? workspace.left : workspace.left + gutter;
        const grow = removedTouchesLeft ? Math.max(0, slot.rect.left - desiredLeft) : removed.rect.width + gutter;
        return {
          ...slot,
          rect: {
            ...slot.rect,
            left: slot.rect.left - grow,
            width: slot.rect.width + grow
          }
        };
      }
      if (side === "left") {
        const desiredRight = isChartSlot(state, slot) ? workspaceRight : workspaceRight - gutter;
        const grow = removedTouchesRight ? Math.max(0, desiredRight - rectRight(slot.rect)) : removed.rect.width + gutter;
        return {
          ...slot,
          rect: {
            ...slot.rect,
            width: slot.rect.width + grow
          }
        };
      }
      if (side === "bottom") {
        const desiredTop = workspace.top + gutter;
        const grow = removedTouchesTop ? Math.max(0, slot.rect.top - desiredTop) : removed.rect.height + gutter;
        return {
          ...slot,
          rect: {
            ...slot.rect,
            top: slot.rect.top - grow,
            height: slot.rect.height + grow
          }
        };
      }
      const desiredBottom = workspaceBottom - gutter;
      const grow = removedTouchesBottom ? Math.max(0, desiredBottom - rectBottom(slot.rect)) : removed.rect.height + gutter;
      return {
        ...slot,
        rect: {
          ...slot.rect,
          height: slot.rect.height + grow
        }
      };
    })
  };
}

function adjacentToRemoved(
  slot: PanelSlot,
  removed: PanelSlot,
  side: "right" | "left" | "bottom" | "top",
  gutter: number
): boolean {
  if (side === "right") {
    return almostEqual(slot.rect.left - rectRight(removed.rect), gutter) &&
      verticalOverlapSlots(slot, removed) > epsilon;
  }
  if (side === "left") {
    return almostEqual(removed.rect.left - rectRight(slot.rect), gutter) &&
      verticalOverlapSlots(slot, removed) > epsilon;
  }
  if (side === "bottom") {
    return almostEqual(slot.rect.top - rectBottom(removed.rect), gutter) &&
      horizontalOverlapSlots(slot, removed) > epsilon;
  }
  return almostEqual(removed.rect.top - rectBottom(slot.rect), gutter) &&
    horizontalOverlapSlots(slot, removed) > epsilon;
}

function removableNeighbor(state: TiledPanelState, removed: PanelSlot, gutter: number): { slot: PanelSlot; sharedLength: number } | null {
  const candidates = state.slots
    .filter((slot) => slot.id !== removed.id)
    .map((slot) => ({ slot, sharedLength: removableSharedLength(slot.rect, removed.rect, gutter) }))
    .filter((item) => item.sharedLength > epsilon)
    .sort((a, b) => b.sharedLength - a.sharedLength);
  return candidates[0] ?? null;
}

function removableSharedLength(candidate: PanelRect, removed: PanelRect, gutter: number): number {
  if (almostEqual(rectRight(candidate) + gutter, removed.left) || almostEqual(rectRight(removed) + gutter, candidate.left)) {
    return Math.max(0, Math.min(rectBottom(candidate), rectBottom(removed)) - Math.max(candidate.top, removed.top));
  }
  if (almostEqual(rectBottom(candidate) + gutter, removed.top) || almostEqual(rectBottom(removed) + gutter, candidate.top)) {
    return Math.max(0, Math.min(rectRight(candidate), rectRight(removed)) - Math.max(candidate.left, removed.left));
  }
  return 0;
}

function unionRect(a: PanelRect, b: PanelRect): PanelRect {
  const left = Math.min(a.left, b.left);
  const top = Math.min(a.top, b.top);
  const right = Math.max(rectRight(a), rectRight(b));
  const bottom = Math.max(rectBottom(a), rectBottom(b));
  return {
    left,
    top,
    width: right - left,
    height: bottom - top
  };
}

function requiredSlot(state: TiledPanelState, slotId: PanelSlotId): PanelSlot {
  const slot = state.slots.find((item) => item.id === slotId);
  if (!slot) {
    throw new Error(`Unknown panel slot: ${slotId}`);
  }
  return slot;
}

function verticalOverlap(a: PanelSlot, b: PanelSlot): { start: number; end: number } {
  return {
    start: Math.max(a.rect.top, b.rect.top),
    end: Math.min(rectBottom(a.rect), rectBottom(b.rect))
  };
}

function horizontalOverlap(a: PanelSlot, b: PanelSlot): { start: number; end: number } {
  return {
    start: Math.max(a.rect.left, b.rect.left),
    end: Math.min(rectRight(a.rect), rectRight(b.rect))
  };
}

function verticalOverlapSlots(a: PanelSlot, b: PanelSlot): number {
  const overlap = verticalOverlap(a, b);
  return overlap.end - overlap.start;
}

function horizontalOverlapSlots(a: PanelSlot, b: PanelSlot): number {
  const overlap = horizontalOverlap(a, b);
  return overlap.end - overlap.start;
}

function hasHorizontalSlotBetween(
  state: TiledPanelState,
  leftSlot: PanelSlot,
  rightSlot: PanelSlot,
  overlap: { start: number; end: number },
  tolerance: number
): boolean {
  const leftRight = rectRight(leftSlot.rect);
  const rightLeft = rightSlot.rect.left;
  return state.slots.some((slot) => (
    slot.id !== leftSlot.id &&
    slot.id !== rightSlot.id &&
    slot.rect.left >= leftRight - tolerance &&
    rectRight(slot.rect) <= rightLeft + tolerance &&
    rangesOverlap(slot.rect.top, rectBottom(slot.rect), overlap.start, overlap.end, tolerance)
  ));
}

function hasVerticalSlotBetween(
  state: TiledPanelState,
  topSlot: PanelSlot,
  bottomSlot: PanelSlot,
  overlap: { start: number; end: number },
  tolerance: number
): boolean {
  const topBottom = rectBottom(topSlot.rect);
  const bottomTop = bottomSlot.rect.top;
  return state.slots.some((slot) => (
    slot.id !== topSlot.id &&
    slot.id !== bottomSlot.id &&
    slot.rect.top >= topBottom - tolerance &&
    rectBottom(slot.rect) <= bottomTop + tolerance &&
    rangesOverlap(slot.rect.left, rectRight(slot.rect), overlap.start, overlap.end, tolerance)
  ));
}

function rangesOverlap(aStart: number, aEnd: number, bStart: number, bEnd: number, tolerance = 0): boolean {
  return Math.min(aEnd, bEnd) - Math.max(aStart, bStart) > tolerance;
}

function rectsOverlap(a: PanelRect, b: PanelRect, tolerance = 0): boolean {
  return rangesOverlap(a.left, rectRight(a), b.left, rectRight(b), tolerance) &&
    rangesOverlap(a.top, rectBottom(a), b.top, rectBottom(b), tolerance);
}

function viewportFromState(state: TiledPanelState): ViewportSize {
  const width = Math.max(...state.slots.map((slot) => rectRight(slot.rect)), 1280);
  const height = Math.max(...state.slots.map((slot) => rectBottom(slot.rect) + workspaceBottomInset), 720);
  return { width, height };
}

function nearestGridLine(x: number, viewport: ViewportSize): GridLineIndex {
  let best: GridLineIndex = 0;
  let bestDistance = Number.POSITIVE_INFINITY;
  ([0, 1, 2, 3, 4] as GridLineIndex[]).forEach((line) => {
    const distance = Math.abs(gridBoundaryX(line, viewport.width) - x);
    if (distance < bestDistance) {
      best = line;
      bestDistance = distance;
    }
  });
  return best;
}

function rectRight(rect: PanelRect): number {
  return rect.left + rect.width;
}

function rectBottom(rect: PanelRect): number {
  return rect.top + rect.height;
}

function almostEqual(a: number, b: number): boolean {
  return Math.abs(a - b) <= epsilon;
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values)].sort();
}

function sortedUnique(values: number[]): number[] {
  return [...new Set(values.map((value) => Math.round(value * 1000) / 1000))].sort((a, b) => a - b);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
