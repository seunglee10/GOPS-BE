import { getPanelDefinition, resolvePanelVariant } from "./panelRegistry";
import type { PanelInstance, PanelPlacement, WorkspaceLayout } from "./types";

export type BoundaryAxis = "x" | "y";

export type BoundaryResizeGuide = {
  id: string;
  axis: BoundaryAxis;
  line: number;
  segmentStart: number;
  segmentSpan: number;
  canDecrease: boolean;
  canIncrease: boolean;
};

type LayoutResult = {
  ok: true;
  layout: WorkspaceLayout;
  message: string;
} | {
  ok: false;
  message: string;
};

const now = () => new Date().toISOString();

function cloneLayout(layout: WorkspaceLayout): WorkspaceLayout {
  return structuredClone(layout) as WorkspaceLayout;
}

function placementEnd(placement: PanelPlacement) {
  return {
    col: placement.col + placement.colSpan - 1,
    row: placement.row + placement.rowSpan - 1
  };
}

function rangesOverlap(startA: number, spanA: number, startB: number, spanB: number): boolean {
  const endA = startA + spanA - 1;
  const endB = startB + spanB - 1;
  return !(endA < startB || endB < startA);
}

function placementArea(placement: PanelPlacement): number {
  return placement.colSpan * placement.rowSpan;
}

function resolveWorkspaceZone(col: number, colSpan: number) {
  if (col === 4 && colSpan === 1) {
    return "context" as const;
  }

  if (col + colSpan - 1 <= 3) {
    return "main" as const;
  }

  return "mainContext" as const;
}

function normalizeWorkspacePlacement(placement: PanelPlacement): PanelPlacement {
  if (placement.group !== "workspace") {
    return placement;
  }

  return {
    ...placement,
    zone: resolveWorkspaceZone(placement.col, placement.colSpan)
  };
}

function withPlacement(panel: PanelInstance, placement: PanelPlacement): PanelInstance {
  const next = {
    ...panel,
    placement: normalizeWorkspacePlacement(placement),
    updatedAt: now()
  };

  return {
    ...next,
    variant: resolvePanelVariant(next)
  };
}

function updatePanel(layout: WorkspaceLayout, panel: PanelInstance): WorkspaceLayout {
  return {
    ...layout,
    panels: layout.panels.map((item) => (item.id === panel.id ? panel : item))
  };
}

function placementContains(container: PanelPlacement, item: PanelPlacement): boolean {
  if (container.group !== item.group) {
    return false;
  }

  const containerEnd = placementEnd(container);
  const itemEnd = placementEnd(item);

  return (
    item.col >= container.col &&
    item.row >= container.row &&
    itemEnd.col <= containerEnd.col &&
    itemEnd.row <= containerEnd.row
  );
}

function coveredCellCount(panels: PanelInstance[]): number {
  const cells = new Set<string>();

  for (const panel of panels) {
    const end = placementEnd(panel.placement);
    for (let col = panel.placement.col; col <= end.col; col += 1) {
      for (let row = panel.placement.row; row <= end.row; row += 1) {
        cells.add(`${panel.placement.group}:${col}:${row}`);
      }
    }
  }

  return cells.size;
}

export function validatePlacement(layout: WorkspaceLayout, panel: PanelInstance, placement = panel.placement): string | null {
  const normalizedPlacement = normalizeWorkspacePlacement(placement);
  const definition = getPanelDefinition(panel.type);
  const zone = normalizedPlacement.zone;

  if (!definition.allowedZones.includes(zone)) {
    return `${definition.title} cannot be placed in ${zone}.`;
  }

  if (normalizedPlacement.group === "agentRail" && zone !== "agentRail") {
    return "Agent rail placements must use agentRail zone.";
  }

  if (zone === "agentRail" && normalizedPlacement.group !== "agentRail") {
    return "agentRail zone cannot be placed in workspace group.";
  }

  if (normalizedPlacement.group === "workspace" && zone === "agentRail") {
    return "Workspace panels cannot span into agentRail.";
  }

  const bounds = layout.zones[normalizedPlacement.group];
  const end = placementEnd(normalizedPlacement);

  if (
    normalizedPlacement.col < 1 ||
    normalizedPlacement.row < 1 ||
    end.col > bounds.columns ||
    end.row > bounds.rows
  ) {
    return `Placement is outside ${normalizedPlacement.group} bounds.`;
  }

  if (zone === "main" && end.col > layout.zones.workspace.mainColumns) {
    return "main panels must stay inside columns 1-3.";
  }

  if (
    zone === "context" &&
    (normalizedPlacement.group !== "workspace" || normalizedPlacement.col !== 4 || normalizedPlacement.colSpan !== 1)
  ) {
    return "context panels must stay in workspace column 4.";
  }

  if (zone === "mainContext" && normalizedPlacement.group !== "workspace") {
    return "mainContext span is only allowed inside workspace.";
  }

  if (
    normalizedPlacement.colSpan < definition.minSpan.colSpan ||
    normalizedPlacement.rowSpan < definition.minSpan.rowSpan
  ) {
    return `${definition.title} is smaller than its minimum span.`;
  }

  if (
    definition.maxSpan &&
    (normalizedPlacement.colSpan > definition.maxSpan.colSpan ||
      normalizedPlacement.rowSpan > definition.maxSpan.rowSpan)
  ) {
    return `${definition.title} is larger than its maximum span.`;
  }

  return null;
}

export function placementsOverlap(a: PanelPlacement, b: PanelPlacement): boolean {
  if (a.group !== b.group) {
    return false;
  }

  const aEnd = placementEnd(a);
  const bEnd = placementEnd(b);

  return !(aEnd.col < b.col || bEnd.col < a.col || aEnd.row < b.row || bEnd.row < a.row);
}

export function collidingPanels(
  panels: PanelInstance[],
  target: PanelInstance,
  placement = target.placement
): PanelInstance[] {
  return panels.filter((panel) => panel.id !== target.id && placementsOverlap(panel.placement, placement));
}

function validateLayout(layout: WorkspaceLayout): string | null {
  for (const panel of layout.panels) {
    const validation = validatePlacement(layout, panel);
    if (validation) {
      return validation;
    }
  }

  for (let index = 0; index < layout.panels.length; index += 1) {
    for (let otherIndex = index + 1; otherIndex < layout.panels.length; otherIndex += 1) {
      if (placementsOverlap(layout.panels[index].placement, layout.panels[otherIndex].placement)) {
        return "Layout would overlap panels.";
      }
    }
  }

  return null;
}

function workspacePanels(layout: WorkspaceLayout): PanelInstance[] {
  return layout.panels.filter((panel) => panel.placement.group === "workspace");
}

function uniqPanels(panels: PanelInstance[]): PanelInstance[] {
  const seen = new Set<string>();
  return panels.filter((panel) => {
    if (seen.has(panel.id)) {
      return false;
    }
    seen.add(panel.id);
    return true;
  });
}

function cellPanelsTouchingBoundary(layout: WorkspaceLayout, axis: BoundaryAxis, line: number, cell: number) {
  const before: PanelInstance[] = [];
  const after: PanelInstance[] = [];

  for (const panel of workspacePanels(layout)) {
    const placement = panel.placement;
    const end = placementEnd(placement);

    if (axis === "x") {
      if (!rangesOverlap(placement.row, placement.rowSpan, cell, 1)) {
        continue;
      }

      if (end.col === line - 1) {
        before.push(panel);
      }

      if (placement.col === line) {
        after.push(panel);
      }
    } else {
      if (!rangesOverlap(placement.col, placement.colSpan, cell, 1)) {
        continue;
      }

      if (end.row === line - 1) {
        before.push(panel);
      }

      if (placement.row === line) {
        after.push(panel);
      }
    }
  }

  return { before, after };
}

function panelsTouchingBoundary(
  layout: WorkspaceLayout,
  axis: BoundaryAxis,
  line: number,
  segmentStart: number,
  segmentSpan: number
) {
  const before: PanelInstance[] = [];
  const after: PanelInstance[] = [];
  const segmentEnd = segmentStart + segmentSpan - 1;

  for (let cell = segmentStart; cell <= segmentEnd; cell += 1) {
    const cellPanels = cellPanelsTouchingBoundary(layout, axis, line, cell);
    before.push(...cellPanels.before);
    after.push(...cellPanels.after);
  }

  return { before: uniqPanels(before), after: uniqPanels(after) };
}

function applyBoundaryDelta(
  layout: WorkspaceLayout,
  axis: BoundaryAxis,
  line: number,
  segmentStart: number,
  segmentSpan: number,
  delta: number
): LayoutResult {
  if (delta === 0) {
    return { ok: false, message: "Boundary delta must not be zero." };
  }

  const { before, after } = panelsTouchingBoundary(layout, axis, line, segmentStart, segmentSpan);
  if (before.length === 0 || after.length === 0) {
    return { ok: false, message: "Boundary resize requires panels on both sides." };
  }

  const affected = [...before, ...after];
  const pinned = affected.find((panel) => panel.layoutPinned);
  if (pinned) {
    return { ok: false, message: `Boundary blocked by pinned panel: ${pinned.title ?? pinned.id}.` };
  }

  let next = cloneLayout(layout);

  for (const panel of before) {
    const placement = panel.placement;
    const nextPlacement = axis === "x"
      ? { ...placement, colSpan: placement.colSpan + delta }
      : { ...placement, rowSpan: placement.rowSpan + delta };
    next = updatePanel(next, withPlacement(panel, nextPlacement));
  }

  for (const panel of after) {
    const placement = panel.placement;
    const nextPlacement = axis === "x"
      ? { ...placement, col: placement.col + delta, colSpan: placement.colSpan - delta }
      : { ...placement, row: placement.row + delta, rowSpan: placement.rowSpan - delta };
    next = updatePanel(next, withPlacement(panel, nextPlacement));
  }

  const invalid = validateLayout(next);
  if (invalid) {
    return { ok: false, message: invalid };
  }

  return { ok: true, layout: next, message: "Boundary resize applied." };
}

function orthogonalStart(axis: BoundaryAxis, panel: PanelInstance): number {
  return axis === "x" ? panel.placement.row : panel.placement.col;
}

function orthogonalSpan(axis: BoundaryAxis, panel: PanelInstance): number {
  return axis === "x" ? panel.placement.rowSpan : panel.placement.colSpan;
}

function closeBoundarySegment(
  layout: WorkspaceLayout,
  axis: BoundaryAxis,
  line: number,
  start: number,
  end: number
): { start: number; span: number } | null {
  const limit = axis === "x" ? layout.zones.workspace.rows : layout.zones.workspace.columns;
  let nextStart = start;
  let nextEnd = end;
  let changed = true;

  while (changed) {
    changed = false;
    const { before, after } = panelsTouchingBoundary(layout, axis, line, nextStart, nextEnd - nextStart + 1);
    const touched = [...before, ...after];

    if (touched.length === 0) {
      return null;
    }

    const panelStart = Math.min(...touched.map((panel) => orthogonalStart(axis, panel)));
    const panelEnd = Math.max(...touched.map((panel) => orthogonalStart(axis, panel) + orthogonalSpan(axis, panel) - 1));
    const clampedStart = Math.max(1, panelStart);
    const clampedEnd = Math.min(limit, panelEnd);

    if (clampedStart !== nextStart || clampedEnd !== nextEnd) {
      nextStart = clampedStart;
      nextEnd = clampedEnd;
      changed = true;
    }
  }

  for (let cell = nextStart; cell <= nextEnd; cell += 1) {
    const { before, after } = cellPanelsTouchingBoundary(layout, axis, line, cell);
    if (before.length === 0 || after.length === 0) {
      return null;
    }
  }

  return { start: nextStart, span: nextEnd - nextStart + 1 };
}

function boundarySegments(layout: WorkspaceLayout, axis: BoundaryAxis, line: number): Array<{ start: number; span: number }> {
  const limit = axis === "x" ? layout.zones.workspace.rows : layout.zones.workspace.columns;
  const segments = new Map<string, { start: number; span: number }>();

  for (let cell = 1; cell <= limit; cell += 1) {
    const { before, after } = cellPanelsTouchingBoundary(layout, axis, line, cell);
    if (before.length === 0 || after.length === 0) {
      continue;
    }

    const segment = closeBoundarySegment(layout, axis, line, cell, cell);
    if (!segment) {
      continue;
    }

    segments.set(`${segment.start}-${segment.span}`, segment);
  }

  return [...segments.values()].sort((left, right) => left.start - right.start || left.span - right.span);
}

export function getBoundaryResizeGuides(layout: WorkspaceLayout): BoundaryResizeGuide[] {
  const guides: BoundaryResizeGuide[] = [];

  for (const line of [2, 3, 4]) {
    for (const segment of boundarySegments(layout, "x", line)) {
      const canDecrease = applyBoundaryDelta(layout, "x", line, segment.start, segment.span, -1).ok;
      const canIncrease = applyBoundaryDelta(layout, "x", line, segment.start, segment.span, 1).ok;

      if (canDecrease || canIncrease) {
        guides.push({
          id: `x-${line}-${segment.start}-${segment.span}`,
          axis: "x",
          line,
          segmentStart: segment.start,
          segmentSpan: segment.span,
          canDecrease,
          canIncrease
        });
      }
    }
  }

  for (const line of [2, 3, 4, 5]) {
    for (const segment of boundarySegments(layout, "y", line)) {
      const canDecrease = applyBoundaryDelta(layout, "y", line, segment.start, segment.span, -1).ok;
      const canIncrease = applyBoundaryDelta(layout, "y", line, segment.start, segment.span, 1).ok;

      if (canDecrease || canIncrease) {
        guides.push({
          id: `y-${line}-${segment.start}-${segment.span}`,
          axis: "y",
          line,
          segmentStart: segment.start,
          segmentSpan: segment.span,
          canDecrease,
          canIncrease
        });
      }
    }
  }

  return guides;
}

export function applyBoundaryResize(
  layout: WorkspaceLayout,
  axis: BoundaryAxis,
  line: number,
  segmentStart: number,
  segmentSpan: number,
  delta: number
): LayoutResult {
  const steps = Math.trunc(delta);
  const direction = steps < 0 ? -1 : steps > 0 ? 1 : 0;
  if (direction === 0) {
    return { ok: false, message: "Boundary delta must not be zero." };
  }

  let next = cloneLayout(layout);
  let nextLine = line;
  let appliedSteps = 0;

  for (let step = 0; step < Math.abs(steps); step += 1) {
    const result = applyBoundaryDelta(next, axis, nextLine, segmentStart, segmentSpan, direction);
    if (!result.ok) {
      break;
    }

    next = result.layout;
    nextLine += direction;
    appliedSteps += 1;
  }

  if (appliedSteps === 0) {
    return { ok: false, message: "Boundary resize is not available." };
  }

  return {
    ok: true,
    layout: next,
    message: appliedSteps === Math.abs(steps)
      ? "Boundary resize applied."
      : "Boundary resized to the nearest available position."
  };
}

function applyPanelMoveStepWithPacking(
  layout: WorkspaceLayout,
  panelId: string,
  requestedPlacement: PanelPlacement
): LayoutResult {
  let next = cloneLayout(layout);
  const panel = next.panels.find((item) => item.id === panelId);

  if (!panel) {
    return { ok: false, message: "Panel not found." };
  }

  if (panel.layoutPinned) {
    return { ok: false, message: "Pinned panels cannot be moved." };
  }

  const normalizedRequested = normalizeWorkspacePlacement({
    ...panel.placement,
    ...requestedPlacement
  });
  const movedPanel = withPlacement(panel, normalizedRequested);
  const validation = validatePlacement(next, movedPanel, normalizedRequested);
  if (validation) {
    return { ok: false, message: validation };
  }

  const collisions = collidingPanels(next.panels, panel, normalizedRequested);
  const pinnedCollision = collisions.find((collision) => collision.layoutPinned);
  if (pinnedCollision) {
    return { ok: false, message: `Move blocked by pinned panel: ${pinnedCollision.title ?? pinnedCollision.id}.` };
  }

  const chunkSwap = applyChunkSwap(next, panel, movedPanel, normalizedRequested, collisions);
  if (chunkSwap.ok) {
    return chunkSwap;
  }

  next = updatePanel(next, movedPanel);

  if (collisions.length > 0) {
    const deltaCol = normalizedRequested.col - panel.placement.col;
    const deltaRow = normalizedRequested.row - panel.placement.row;
    const axis: BoundaryAxis = Math.abs(deltaCol) >= Math.abs(deltaRow) ? "x" : "y";
    const shift = axis === "x"
      ? (deltaCol < 0 ? panel.placement.colSpan : -panel.placement.colSpan)
      : (deltaRow < 0 ? panel.placement.rowSpan : -panel.placement.rowSpan);

    for (const collision of collisions) {
      const placement = collision.placement;
      const nextPlacement = axis === "x"
        ? { ...placement, col: placement.col + shift }
        : { ...placement, row: placement.row + shift };
      next = updatePanel(next, withPlacement(collision, nextPlacement));
    }
  }

  const invalid = validateLayout(next);
  if (invalid) {
    return { ok: false, message: "Move is not packable in the current grid." };
  }

  return { ok: true, layout: next, message: collisions.length ? "Panel moved with packed group." : "Panel moved." };
}

function applyChunkSwap(
  layout: WorkspaceLayout,
  panel: PanelInstance,
  movedPanel: PanelInstance,
  targetPlacement: PanelPlacement,
  collisions: PanelInstance[]
): LayoutResult {
  const chunkPanels = layout.panels.filter((item) =>
    item.id !== panel.id && placementContains(targetPlacement, item.placement)
  );

  if (chunkPanels.length === 0 || collisions.length === 0) {
    return { ok: false, message: "Chunk swap requires collisions." };
  }

  if (chunkPanels.some((chunkPanel) => chunkPanel.layoutPinned)) {
    return { ok: false, message: "Chunk swap is blocked by a pinned panel." };
  }

  if (coveredCellCount(chunkPanels) !== placementArea(targetPlacement)) {
    return { ok: false, message: "Chunk swap requires the target area to be fully covered." };
  }

  let next = updatePanel(layout, movedPanel);
  const sourcePlacement = panel.placement;

  for (const chunkPanel of chunkPanels) {
    const placement = chunkPanel.placement;
    const nextPlacement = normalizeWorkspacePlacement({
      ...placement,
      col: sourcePlacement.col + (placement.col - targetPlacement.col),
      row: sourcePlacement.row + (placement.row - targetPlacement.row)
    });
    const nextCollision = withPlacement(chunkPanel, nextPlacement);
    const validation = validatePlacement(next, nextCollision, nextPlacement);
    if (validation) {
      return { ok: false, message: validation };
    }

    next = updatePanel(next, nextCollision);
  }

  const invalid = validateLayout(next);
  if (invalid) {
    return { ok: false, message: invalid };
  }

  return { ok: true, layout: next, message: "Panel swapped with panel group." };
}


function placementDistance(a: PanelPlacement, b: PanelPlacement): number {
  return Math.abs(a.col - b.col) + Math.abs(a.row - b.row);
}

function candidatePlacementsForPanel(
  layout: WorkspaceLayout,
  panel: PanelInstance,
  placedPanels: PanelInstance[]
): PanelPlacement[] {
  const bounds = layout.zones[panel.placement.group];
  const maxCol = bounds.columns - panel.placement.colSpan + 1;
  const maxRow = bounds.rows - panel.placement.rowSpan + 1;
  const candidates: PanelPlacement[] = [];

  for (let row = 1; row <= maxRow; row += 1) {
    for (let col = 1; col <= maxCol; col += 1) {
      const placement = normalizeWorkspacePlacement({
        ...panel.placement,
        col,
        row
      });
      const candidate = withPlacement(panel, placement);
      if (validatePlacement(layout, candidate, placement)) {
        continue;
      }
      if (placedPanels.some((placed) => placementsOverlap(placed.placement, placement))) {
        continue;
      }
      candidates.push(placement);
    }
  }

  return candidates.sort((left, right) =>
    placementDistance(left, panel.placement) - placementDistance(right, panel.placement) ||
    left.row - right.row ||
    left.col - right.col
  );
}

function placeRelocatedPanels(
  layout: WorkspaceLayout,
  relocatingPanels: PanelInstance[],
  placedPanels: PanelInstance[],
  index = 0
): PanelInstance[] | null {
  if (index >= relocatingPanels.length) {
    return [];
  }

  const panel = relocatingPanels[index];
  for (const placement of candidatePlacementsForPanel(layout, panel, placedPanels)) {
    const relocated = withPlacement(panel, placement);
    const rest = placeRelocatedPanels(layout, relocatingPanels, [...placedPanels, relocated], index + 1);
    if (rest) {
      return [relocated, ...rest];
    }
  }

  return null;
}

function applyFreeRelocationPacking(
  layout: WorkspaceLayout,
  panelId: string,
  requestedPlacement: PanelPlacement
): LayoutResult {
  const next = cloneLayout(layout);
  const panel = next.panels.find((item) => item.id === panelId);
  if (!panel) {
    return { ok: false, message: "Panel not found." };
  }

  if (panel.layoutPinned) {
    return { ok: false, message: "Pinned panels cannot be moved." };
  }

  const target = normalizeWorkspacePlacement({
    ...panel.placement,
    ...requestedPlacement
  });
  const movedPanel = withPlacement(panel, target);
  const validation = validatePlacement(next, movedPanel, target);
  if (validation) {
    return { ok: false, message: validation };
  }

  const collisions = collidingPanels(next.panels, panel, target);
  const pinnedCollision = collisions.find((collision) => collision.layoutPinned);
  if (pinnedCollision) {
    return { ok: false, message: `Move blocked by pinned panel: ${pinnedCollision.title ?? pinnedCollision.id}.` };
  }

  const relocatingPanels = collisions.sort((left, right) => placementArea(right.placement) - placementArea(left.placement));
  const placedPanels = next.panels.filter(
    (item) => item.id !== panel.id && !relocatingPanels.some((collision) => collision.id === item.id)
  );
  const relocatedPanels = placeRelocatedPanels(next, relocatingPanels, [movedPanel, ...placedPanels]);
  if (!relocatedPanels) {
    return { ok: false, message: "Move is not freely packable in the current grid." };
  }

  let result = updatePanel(next, movedPanel);
  for (const relocated of relocatedPanels) {
    result = updatePanel(result, relocated);
  }

  const invalid = validateLayout(result);
  if (invalid) {
    return { ok: false, message: invalid };
  }

  return {
    ok: true,
    layout: result,
    message: relocatedPanels.length ? "Panel moved with free packing." : "Panel moved."
  };
}


function targetPlacementsNear(
  layout: WorkspaceLayout,
  panel: PanelInstance,
  target: PanelPlacement
): PanelPlacement[] {
  const bounds = layout.zones[panel.placement.group];
  const maxCol = bounds.columns - target.colSpan + 1;
  const maxRow = bounds.rows - target.rowSpan + 1;
  const placements: PanelPlacement[] = [];

  for (let row = 1; row <= maxRow; row += 1) {
    for (let col = 1; col <= maxCol; col += 1) {
      const placement = normalizeWorkspacePlacement({
        ...target,
        col,
        row
      });
      const candidate = withPlacement(panel, placement);
      if (!validatePlacement(layout, candidate, placement)) {
        placements.push(placement);
      }
    }
  }

  return placements.sort((left, right) =>
    placementDistance(left, target) - placementDistance(right, target) ||
    placementDistance(left, panel.placement) - placementDistance(right, panel.placement) ||
    left.row - right.row ||
    left.col - right.col
  );
}

function applyNearestFreePacking(
  layout: WorkspaceLayout,
  panelId: string,
  target: PanelPlacement
): LayoutResult {
  const panel = layout.panels.find((item) => item.id === panelId);
  if (!panel) {
    return { ok: false, message: "Panel not found." };
  }

  for (const placement of targetPlacementsNear(layout, panel, target)) {
    if (placement.col === panel.placement.col && placement.row === panel.placement.row) {
      continue;
    }

    const directResult = applyPanelMoveStepWithPacking(layout, panelId, placement);
    if (directResult.ok) {
      return directResult;
    }

    const freePackingResult = applyFreeRelocationPacking(layout, panelId, placement);
    if (freePackingResult.ok) {
      return freePackingResult;
    }
  }

  return { ok: false, message: "No nearby packable placement is available." };
}

export function applyPanelMoveWithPacking(
  layout: WorkspaceLayout,
  panelId: string,
  requestedPlacement: PanelPlacement
): LayoutResult {
  let next = cloneLayout(layout);
  let panel = next.panels.find((item) => item.id === panelId);

  if (!panel) {
    return { ok: false, message: "Panel not found." };
  }

  const target = normalizeWorkspacePlacement({
    ...panel.placement,
    ...requestedPlacement
  });
  const directResult = applyPanelMoveStepWithPacking(next, panelId, target);
  if (directResult.ok) {
    return directResult;
  }

  const freePackingResult = applyFreeRelocationPacking(next, panelId, target);
  if (freePackingResult.ok) {
    return freePackingResult;
  }

  const nearestFreePackingResult = applyNearestFreePacking(next, panelId, target);
  if (nearestFreePackingResult.ok) {
    return nearestFreePackingResult;
  }

  let moved = false;

  while (panel.placement.col !== target.col || panel.placement.row !== target.row) {
    const deltaCol = target.col - panel.placement.col;
    const deltaRow = target.row - panel.placement.row;
    const moveColumnFirst = Math.abs(deltaCol) >= Math.abs(deltaRow);
    const nextPlacement = {
      ...panel.placement,
      col: panel.placement.col + (moveColumnFirst && deltaCol !== 0 ? Math.sign(deltaCol) : 0),
      row: panel.placement.row + (!moveColumnFirst && deltaRow !== 0 ? Math.sign(deltaRow) : 0)
    };

    if (nextPlacement.col === panel.placement.col && deltaCol !== 0) {
      nextPlacement.col += Math.sign(deltaCol);
    }

    if (nextPlacement.row === panel.placement.row && deltaRow !== 0) {
      nextPlacement.row += Math.sign(deltaRow);
    }

    const result = applyPanelMoveStepWithPacking(next, panelId, nextPlacement);
    if (!result.ok) {
      return moved
        ? { ok: true, layout: next, message: "Panel moved to the nearest packable position." }
        : result;
    }

    next = result.layout;
    moved = true;
    panel = next.panels.find((item) => item.id === panelId);
    if (!panel) {
      return { ok: false, message: "Panel not found after move." };
    }
  }

  return { ok: true, layout: next, message: moved ? "Panel moved with packed group." : "Panel moved." };
}

type GridCell = { col: number; row: number };

type ExpansionCandidate = {
  panel: PanelInstance;
  placement: PanelPlacement;
  priority: number;
};

function workspaceCells(layout: WorkspaceLayout): GridCell[] {
  const cells: GridCell[] = [];
  const bounds = layout.zones.workspace;

  for (let row = 1; row <= bounds.rows; row += 1) {
    for (let col = 1; col <= bounds.columns; col += 1) {
      cells.push({ col, row });
    }
  }

  return cells;
}

function placementCoversCell(placement: PanelPlacement, cell: GridCell): boolean {
  if (placement.group !== "workspace") {
    return false;
  }

  const end = placementEnd(placement);
  return cell.col >= placement.col && cell.col <= end.col && cell.row >= placement.row && cell.row <= end.row;
}

function isWorkspaceCellEmpty(layout: WorkspaceLayout, cell: GridCell): boolean {
  return !layout.panels.some((panel) => placementCoversCell(panel.placement, cell));
}

function findEmptyWorkspaceCell(layout: WorkspaceLayout): GridCell | null {
  return workspaceCells(layout).find((cell) => isWorkspaceCellEmpty(layout, cell)) ?? null;
}

function isPanelExpansionValid(layout: WorkspaceLayout, panel: PanelInstance, placement: PanelPlacement): boolean {
  const candidate = withPlacement(panel, placement);
  if (validatePlacement(layout, candidate, placement)) {
    return false;
  }

  if (collidingPanels(layout.panels, panel, placement).length > 0) {
    return false;
  }

  const next = updatePanel(layout, candidate);
  return validateLayout(next) === null;
}

function expansionCandidatesForCell(layout: WorkspaceLayout, cell: GridCell): ExpansionCandidate[] {
  const candidates: ExpansionCandidate[] = [];

  for (const panel of workspacePanels(layout)) {
    if (panel.layoutPinned) {
      continue;
    }

    const placement = panel.placement;
    const end = placementEnd(placement);
    const coversColumn = cell.col >= placement.col && cell.col <= end.col;
    const coversRow = cell.row >= placement.row && cell.row <= end.row;

    const maybeAdd = (nextPlacement: PanelPlacement, priority: number) => {
      const normalized = normalizeWorkspacePlacement(nextPlacement);
      if (!isPanelExpansionValid(layout, panel, normalized)) {
        return;
      }
      candidates.push({ panel, placement: normalized, priority });
    };

    if (coversColumn && end.row === cell.row - 1) {
      maybeAdd({ ...placement, rowSpan: placement.rowSpan + 1 }, 0);
    }

    if (coversRow && end.col === cell.col - 1) {
      maybeAdd({ ...placement, colSpan: placement.colSpan + 1 }, 1);
    }

    if (coversColumn && placement.row === cell.row + 1) {
      maybeAdd({ ...placement, row: cell.row, rowSpan: placement.rowSpan + 1 }, 2);
    }

    if (coversRow && placement.col === cell.col + 1) {
      maybeAdd({ ...placement, col: cell.col, colSpan: placement.colSpan + 1 }, 3);
    }
  }

  return candidates.sort((left, right) =>
    left.priority - right.priority ||
    (right.panel.layoutWeight ?? 0) - (left.panel.layoutWeight ?? 0) ||
    placementArea(right.panel.placement) - placementArea(left.panel.placement) ||
    left.panel.placement.row - right.panel.placement.row ||
    left.panel.placement.col - right.panel.placement.col
  );
}

function fillWorkspaceVacancies(layout: WorkspaceLayout): WorkspaceLayout {
  let next = cloneLayout(layout);
  const maxIterations = layout.zones.workspace.columns * layout.zones.workspace.rows;

  for (let iteration = 0; iteration < maxIterations; iteration += 1) {
    const emptyCell = findEmptyWorkspaceCell(next);
    if (!emptyCell) {
      return next;
    }

    const [candidate] = expansionCandidatesForCell(next, emptyCell);
    if (!candidate) {
      return next;
    }

    next = updatePanel(next, withPlacement(candidate.panel, candidate.placement));
  }

  return next;
}

export function reflowLayout(layout: WorkspaceLayout): LayoutResult {
  const invalid = validateLayout(layout);
  if (invalid) {
    return { ok: false, message: invalid };
  }

  const nextLayout = fillWorkspaceVacancies(layout);
  const nextInvalid = validateLayout(nextLayout);
  if (nextInvalid) {
    return { ok: false, message: nextInvalid };
  }

  return {
    ok: true,
    layout: nextLayout,
    message: layout === nextLayout ? "Layout is already valid." : "Layout reflowed into open space."
  };
}
