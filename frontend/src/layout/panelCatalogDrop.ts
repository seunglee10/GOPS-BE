import { makeCommand } from "./commands";
import { columnLinePercent, workspaceColumnCount, workspaceColumnStarts, workspaceRowCount } from "./gridGeometry";
import type { LayoutCommand, PanelInstance, PanelPlacement, PanelType, WorkspaceLayout } from "./types";

export const PANEL_CATALOG_MIME = "application/x-gops-panel-type";

export const PANEL_CATALOG_TYPES: PanelType[] = [
  "chart",
  "newsFeed",
  "symbolSummary",
  "aiSummary",
  "watchlist",
  "indicatorCompare",
  "proposalReview",
  "notifications"
];

export type WorkspaceDropCell = {
  col: number;
  row: number;
};

type FrameRectLike = Pick<DOMRectReadOnly, "left" | "top" | "width" | "height">;

type PanelDropCommandInput = {
  layout: WorkspaceLayout;
  panelType: PanelType;
  activeSymbol: string;
  cell?: WorkspaceDropCell | null;
  targetPanelId?: string | null;
};

export type PanelDropPreview =
  | {
    kind: "add";
    panelType: PanelType;
    placement: PanelPlacement;
  }
  | {
    kind: "replace";
    panelType: PanelType;
    panelId: string;
    placement: PanelPlacement;
  }
  | {
    kind: "blocked";
    panelType: PanelType;
    panelId?: string;
    placement?: PanelPlacement;
    reason: string;
  };

export function getWorkspaceDropCell(
  frameRect: FrameRectLike,
  clientX: number,
  clientY: number
): WorkspaceDropCell | null {
  const x = clientX - frameRect.left;
  const y = clientY - frameRect.top;
  const workspaceRight = (columnLinePercent(workspaceColumnCount + 1) / 100) * frameRect.width;

  if (x < 0 || y < 0 || x > workspaceRight || y > frameRect.height || frameRect.width <= 0 || frameRect.height <= 0) {
    return null;
  }

  const columnStarts = workspaceColumnStarts(frameRect.width);
  let col = 0;
  for (let index = 0; index < workspaceColumnCount; index += 1) {
    const start = columnStarts[index] ?? 0;
    const end = index === workspaceColumnCount - 1 ? workspaceRight : columnStarts[index + 1] ?? workspaceRight;
    if (x >= start && x <= end) {
      col = index + 1;
      break;
    }
  }

  if (col === 0) {
    return null;
  }

  const rowHeight = frameRect.height / workspaceRowCount;
  const row = Math.min(workspaceRowCount, Math.max(1, Math.floor(y / rowHeight) + 1));
  return { col, row };
}

export function findWorkspacePanelAtCell(layout: WorkspaceLayout, cell: WorkspaceDropCell): PanelInstance | null {
  return layout.panels.find((panel) => {
    if (panel.placement.group !== "workspace") {
      return false;
    }

    return (
      cell.col >= panel.placement.col &&
      cell.col < panel.placement.col + panel.placement.colSpan &&
      cell.row >= panel.placement.row &&
      cell.row < panel.placement.row + panel.placement.rowSpan
    );
  }) ?? null;
}

export function findMaxEmptyWorkspaceRect(layout: WorkspaceLayout, cell: WorkspaceDropCell): PanelPlacement | null {
  if (findWorkspacePanelAtCell(layout, cell)) {
    return null;
  }

  let best: PanelPlacement | null = null;

  for (let row = 1; row <= workspaceRowCount; row += 1) {
    for (let col = 1; col <= workspaceColumnCount; col += 1) {
      for (let rowSpan = 1; row + rowSpan - 1 <= workspaceRowCount; rowSpan += 1) {
        for (let colSpan = 1; col + colSpan - 1 <= workspaceColumnCount; colSpan += 1) {
          const candidate: PanelPlacement = {
            group: "workspace",
            zone: resolveWorkspaceZone(col, colSpan),
            col,
            row,
            colSpan,
            rowSpan
          };

          if (!rectContainsCell(candidate, cell) || !isWorkspaceRectEmpty(layout, candidate)) {
            continue;
          }

          if (!best || comparePlacementCandidate(candidate, best) < 0) {
            best = candidate;
          }
        }
      }
    }
  }

  return best;
}

export function createPanelDropCommand({
  layout,
  panelType,
  activeSymbol,
  cell,
  targetPanelId
}: PanelDropCommandInput): LayoutCommand | null {
  const preview = createPanelDropPreview({ layout, panelType, activeSymbol, cell, targetPanelId });
  if (!preview || preview.kind === "blocked") {
    return null;
  }
  const props = panelType === "chart" ? { symbol: activeSymbol } : {};

  if (preview.kind === "replace") {
    return makeCommand(
      "layout.panel.replace",
      "user",
      { panelId: preview.panelId, panelType, props },
      { panelId: preview.panelId, group: preview.placement.group, zone: preview.placement.zone }
    );
  }

  return makeCommand(
    "layout.panel.add",
    "user",
    { panelType, placement: preview.placement, props },
    { group: preview.placement.group, zone: preview.placement.zone }
  );
}

export function createPanelDropPreview({
  layout,
  panelType,
  cell,
  targetPanelId
}: PanelDropCommandInput): PanelDropPreview | null {
  if (!PANEL_CATALOG_TYPES.includes(panelType)) {
    return null;
  }

  const targetPanel = findWorkspacePanelById(layout, targetPanelId) ?? (cell ? findWorkspacePanelAtCell(layout, cell) : null);
  if (targetPanel) {
    if (targetPanel.layoutPinned) {
      return {
        kind: "blocked",
        panelType,
        panelId: targetPanel.id,
        placement: targetPanel.placement,
        reason: "Pinned panels cannot be replaced."
      };
    }

    return {
      kind: "replace",
      panelType,
      panelId: targetPanel.id,
      placement: targetPanel.placement
    };
  }

  if (!cell) {
    return null;
  }

  const placement = findMaxEmptyWorkspaceRect(layout, cell);
  if (!placement) {
    return {
      kind: "blocked",
      panelType,
      reason: "No available workspace cell."
    };
  }

  return {
    kind: "add",
    panelType,
    placement
  };
}

function findWorkspacePanelById(layout: WorkspaceLayout, panelId?: string | null): PanelInstance | null {
  if (!panelId) {
    return null;
  }

  return layout.panels.find((panel) => panel.id === panelId && panel.placement.group === "workspace") ?? null;
}

function isWorkspaceRectEmpty(layout: WorkspaceLayout, rect: PanelPlacement): boolean {
  for (let row = rect.row; row < rect.row + rect.rowSpan; row += 1) {
    for (let col = rect.col; col < rect.col + rect.colSpan; col += 1) {
      if (findWorkspacePanelAtCell(layout, { col, row })) {
        return false;
      }
    }
  }

  return true;
}

function rectContainsCell(rect: PanelPlacement, cell: WorkspaceDropCell): boolean {
  return (
    cell.col >= rect.col &&
    cell.col < rect.col + rect.colSpan &&
    cell.row >= rect.row &&
    cell.row < rect.row + rect.rowSpan
  );
}

function comparePlacementCandidate(left: PanelPlacement, right: PanelPlacement): number {
  const leftArea = left.colSpan * left.rowSpan;
  const rightArea = right.colSpan * right.rowSpan;

  if (leftArea !== rightArea) {
    return rightArea - leftArea;
  }

  if (left.colSpan !== right.colSpan) {
    return right.colSpan - left.colSpan;
  }

  if (left.row !== right.row) {
    return left.row - right.row;
  }

  return left.col - right.col;
}

function resolveWorkspaceZone(col: number, colSpan: number): PanelPlacement["zone"] {
  if (col === 4 && colSpan === 1) {
    return "context";
  }

  if (col + colSpan - 1 <= 3) {
    return "main";
  }

  return "mainContext";
}
