import type { PointerEvent as ReactPointerEvent } from "react";
import { makeCommand } from "../layout/commands";
import {
  columnLinePercent,
  columnSpanPercent,
  rowLinePercent,
  workspaceColumnCount,
  workspaceRowCount
} from "../layout/gridGeometry";
import { applyBoundaryResize, getBoundaryResizeGuides, type BoundaryResizeGuide } from "../layout/reflow";
import type { LayoutCommand, LayoutPreviewItem, WorkspaceLayout } from "../layout/types";

type BoundaryResizeOverlayProps = {
  layout: WorkspaceLayout;
  onCommand: (command: LayoutCommand) => void;
  onPreviewChange: (preview: LayoutPreviewItem[]) => void;
};

function guideStyle(guide: BoundaryResizeGuide) {
  if (guide.axis === "x") {
    return {
      left: `${columnLinePercent(guide.line)}%`,
      top: `${rowLinePercent(guide.segmentStart)}%`,
      height: `${(guide.segmentSpan / workspaceRowCount) * 100}%`
    };
  }

  return {
    left: `${columnLinePercent(guide.segmentStart)}%`,
    top: `${rowLinePercent(guide.line)}%`,
    width: `${columnSpanPercent(guide.segmentStart, guide.segmentSpan)}%`
  };
}

function nearestLine(lines: number[], value: number): number {
  let bestLine = 1;
  let bestDistance = Number.POSITIVE_INFINITY;

  lines.forEach((linePosition, index) => {
    const distance = Math.abs(linePosition - value);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestLine = index + 1;
    }
  });

  return bestLine;
}

function linePositions(axis: BoundaryResizeGuide["axis"], rect: DOMRect): number[] {
  if (axis === "x") {
    return Array.from({ length: workspaceColumnCount + 1 }, (_, index) =>
      (columnLinePercent(index + 1) / 100) * rect.width
    );
  }

  return Array.from({ length: workspaceRowCount + 1 }, (_, index) =>
    (rowLinePercent(index + 1) / 100) * rect.height
  );
}

function resolveDelta(
  guide: BoundaryResizeGuide,
  frameRect: DOMRect,
  startX: number,
  startY: number,
  latestX: number,
  latestY: number
) {
  const rawDelta = guide.axis === "x" ? latestX - startX : latestY - startY;
  const lines = linePositions(guide.axis, frameRect);
  const startLinePosition = lines[guide.line - 1];
  const requested = nearestLine(lines, startLinePosition + rawDelta) - guide.line;

  if (requested < 0 && guide.canDecrease) {
    return requested;
  }

  if (requested > 0 && guide.canIncrease) {
    return requested;
  }

  return 0;
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

export function BoundaryResizeOverlay({ layout, onCommand, onPreviewChange }: BoundaryResizeOverlayProps) {
  const guides = getBoundaryResizeGuides(layout);

  const beginDrag = (guide: BoundaryResizeGuide, event: ReactPointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) {
      return;
    }

    event.preventDefault();
    event.stopPropagation();
    const target = event.currentTarget;
    const frame = target.closest(".layout-frame");
    if (!(frame instanceof HTMLElement)) {
      return;
    }
    const frameRect = frame.getBoundingClientRect();
    target.setPointerCapture?.(event.pointerId);

    const startX = event.clientX;
    const startY = event.clientY;
    let latestX = startX;
    let latestY = startY;
    let finished = false;

    const updatePreview = (clientX: number, clientY: number) => {
      const delta = resolveDelta(guide, frameRect, startX, startY, clientX, clientY);
      if (delta === 0) {
        onPreviewChange([]);
        return delta;
      }

      const result = applyBoundaryResize(layout, guide.axis, guide.line, guide.segmentStart, guide.segmentSpan, delta);
      onPreviewChange(result.ok ? changedPanelPreview(layout, result.layout) : []);
      return delta;
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
        target.releasePointerCapture?.(event.pointerId);
      } catch {
        // Capture may already be released by the browser.
      }

      target.removeEventListener("pointermove", handlePointerMove);
      target.removeEventListener("pointerup", handlePointerUp);
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      onPreviewChange([]);

      const delta = updatePreview(latestX, latestY);
      onPreviewChange([]);
      if (delta === 0) {
        return;
      }

      onCommand(makeCommand("layout.boundary.resize", "user", {
        group: "workspace",
        axis: guide.axis,
        line: guide.line,
        segmentStart: guide.segmentStart,
        segmentSpan: guide.segmentSpan,
        delta
      }));
    };

    target.addEventListener("pointermove", handlePointerMove);
    target.addEventListener("pointerup", handlePointerUp, { once: true });
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp, { once: true });
  };

  return (
    <div className="boundary-overlay" aria-label="Resizable panel boundaries">
      {guides.map((guide) => (
        <button
          key={guide.id}
          type="button"
          className={`boundary-guide ${guide.axis === "x" ? "vertical" : "horizontal"}`}
          style={guideStyle(guide)}
          title="Resize shared panel boundary"
          onPointerDown={(event) => beginDrag(guide, event)}
        >
          <span />
        </button>
      ))}
    </div>
  );
}
