import { Pin, PinOff, Trash2 } from "lucide-react";
import { useState } from "react";
import type { CSSProperties, PointerEvent as ReactPointerEvent } from "react";
import { makeCommand } from "../layout/commands";
import { workspaceColumnCount, workspaceColumnStarts, workspaceRowCount, workspaceRowStarts } from "../layout/gridGeometry";
import { applyPanelMoveWithPacking } from "../layout/reflow";
import type { LayoutCommand, LayoutPreviewItem, PanelInstance, WorkspaceLayout } from "../layout/types";

type PanelCardProps = {
  layout: WorkspaceLayout;
  panel: PanelInstance;
  selected: boolean;
  style: CSSProperties;
  onCommand: (command: LayoutCommand) => void;
  onPreviewChange: (preview: LayoutPreviewItem[]) => void;
};

function PanelBody({ panel }: { panel: PanelInstance }) {
  return (
    <div className="panel-dummy">
      <span>{panel.title ?? panel.type}</span>
      <small>Dummy panel content</small>
    </div>
  );
}

function getGridMetrics(target: EventTarget | null) {
  const element = target instanceof Element ? target : null;
  const frame = element?.closest(".layout-frame");
  if (!frame) {
    return null;
  }

  const rect = frame.getBoundingClientRect();
  return {
    columnStarts: workspaceColumnStarts(rect.width),
    rowStarts: workspaceRowStarts(rect.height)
  };
}

function nearestStartIndex(starts: number[], value: number, maxIndex: number): number {
  let bestIndex = 0;
  let bestDistance = Number.POSITIVE_INFINITY;

  for (let index = 0; index <= maxIndex; index += 1) {
    const distance = Math.abs(starts[index] - value);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestIndex = index;
    }
  }

  return bestIndex;
}

function changedPanelPreview(current: WorkspaceLayout, next: WorkspaceLayout): LayoutPreviewItem[] {
  return next.panels.flatMap((nextPanel) => {
    const currentPanel = current.panels.find((item) => item.id === nextPanel.id);
    if (!currentPanel || JSON.stringify(currentPanel.placement) === JSON.stringify(nextPanel.placement)) {
      return [];
    }

    return [{ panelId: nextPanel.id, placement: nextPanel.placement }];
  });
}

export function PanelCard({ layout, panel, selected, style, onCommand, onPreviewChange }: PanelCardProps) {
  const [dragging, setDragging] = useState(false);
  const commandTarget = { panelId: panel.id, group: panel.placement.group, zone: panel.placement.zone };

  const runPanelCommand = (type: LayoutCommand["type"], payload: Record<string, unknown> = {}) => {
    onCommand(makeCommand(type, "user", { panelId: panel.id, ...payload }, commandTarget));
  };

  const beginDrag = (event: ReactPointerEvent<HTMLElement>) => {
    if (event.button !== 0 || panel.layoutPinned || panel.placement.group !== "workspace") {
      return;
    }

    const metrics = getGridMetrics(event.currentTarget);
    if (!metrics) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();
    const interactionTarget = event.currentTarget;
    interactionTarget.setPointerCapture?.(event.pointerId);
    setDragging(true);

    const startX = event.clientX;
    const startY = event.clientY;
    const startPlacement = panel.placement;
    let latestX = startX;
    let latestY = startY;
    let finished = false;

    const updatePreview = (clientX: number, clientY: number) => {
      const startColPx = metrics.columnStarts[startPlacement.col - 1];
      const startRowPx = metrics.rowStarts[startPlacement.row - 1];
      const nextCol = nearestStartIndex(
        metrics.columnStarts,
        startColPx + clientX - startX,
        workspaceColumnCount - startPlacement.colSpan
      ) + 1;
      const nextRow = nearestStartIndex(
        metrics.rowStarts,
        startRowPx + clientY - startY,
        workspaceRowCount - startPlacement.rowSpan
      ) + 1;

      if (nextCol === startPlacement.col && nextRow === startPlacement.row) {
        onPreviewChange([]);
        return { col: nextCol, row: nextRow };
      }

      const result = applyPanelMoveWithPacking(layout, panel.id, {
        ...startPlacement,
        col: nextCol,
        row: nextRow
      });
      onPreviewChange(result.ok ? changedPanelPreview(layout, result.layout) : []);
      return { col: nextCol, row: nextRow };
    };

    const handlePointerMove = (moveEvent: PointerEvent) => {
      latestX = moveEvent.clientX;
      latestY = moveEvent.clientY;
      updatePreview(latestX, latestY);
    };

    const handlePointerUp = () => {
      if (finished) {
        return;
      }
      finished = true;

      try {
        interactionTarget.releasePointerCapture?.(event.pointerId);
      } catch {
        // Capture may already be released by the browser.
      }

      interactionTarget.removeEventListener("pointermove", handlePointerMove);
      interactionTarget.removeEventListener("pointerup", handlePointerUp);
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      setDragging(false);
      onPreviewChange([]);

      const { col: nextCol, row: nextRow } = updatePreview(latestX, latestY);
      onPreviewChange([]);

      if (nextCol === startPlacement.col && nextRow === startPlacement.row) {
        return;
      }

      runPanelCommand("layout.panel.move", {
        placement: {
          ...startPlacement,
          col: nextCol,
          row: nextRow
        }
      });
    };

    interactionTarget.addEventListener("pointermove", handlePointerMove);
    interactionTarget.addEventListener("pointerup", handlePointerUp, { once: true });
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp, { once: true });
  };

  return (
    <article
      className={`panel-card ${selected ? "selected" : ""} ${dragging ? "dragging" : ""} ${panel.layoutPinned ? "pinned" : ""}`}
      style={style}
      onClick={() => runPanelCommand("layout.panel.select")}
    >
      <header className="panel-header" onPointerDown={beginDrag}>
        <div className="panel-title-block">
          <strong>{panel.title}</strong>
          <span>
            {panel.variant} / {panel.placement.zone}
          </span>
        </div>
        <div className="panel-actions" onPointerDown={(event) => event.stopPropagation()}>
          <button
            title={panel.layoutPinned ? "Unpin" : "Pin"}
            onClick={(event) => {
              event.stopPropagation();
              runPanelCommand(panel.layoutPinned ? "layout.panel.unpin" : "layout.panel.pin");
            }}
          >
            {panel.layoutPinned ? <PinOff size={14} /> : <Pin size={14} />}
          </button>
          <button
            title="Remove"
            onClick={(event) => {
              event.stopPropagation();
              runPanelCommand("layout.panel.remove");
            }}
          >
            <Trash2 size={14} />
          </button>
        </div>
      </header>

      <PanelBody panel={panel} />
    </article>
  );
}
