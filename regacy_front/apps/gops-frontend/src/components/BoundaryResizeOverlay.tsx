import { type PointerEvent as ReactPointerEvent } from "react";
import {
  columnLinePercent,
  columnSpanPercent,
  rowLinePercent,
  workspaceColumnCount,
  workspaceRowCount
} from "../layout/gridGeometry";
import { getBoundaryResizeGuides, type BoundaryResizeGuide } from "../layout/reflow";
import type { LayoutPreviewItem, WorkspaceLayout } from "../layout/types";

export type ContinuousGridTracks = {
  columns?: number[];
  rows?: number[];
};

type BoundaryResizeOverlayProps = {
  layout: WorkspaceLayout;
  onPreviewChange: (preview: LayoutPreviewItem[]) => void;
  onTrackResize: (axis: BoundaryResizeGuide["axis"], tracks: number[]) => void;
  systemColumnVisible: boolean;
  tracks: ContinuousGridTracks;
};

function trackLinePercent(tracks: number[] | undefined, line: number, fallback: () => number): number {
  if (!tracks?.length) {
    return fallback();
  }

  const total = tracks.reduce((sum, track) => sum + track, 0);
  if (total <= 0) {
    return fallback();
  }

  return (tracks.slice(0, Math.max(0, line - 1)).reduce((sum, track) => sum + track, 0) / total) * 100;
}

function trackSpanPercent(tracks: number[] | undefined, start: number, span: number, fallback: () => number): number {
  if (!tracks?.length) {
    return fallback();
  }

  const total = tracks.reduce((sum, track) => sum + track, 0);
  if (total <= 0) {
    return fallback();
  }

  return (tracks.slice(Math.max(0, start - 1), Math.max(0, start - 1 + span)).reduce((sum, track) => sum + track, 0) / total) * 100;
}

function guideStyle(guide: BoundaryResizeGuide, tracks: ContinuousGridTracks, systemColumnVisible: boolean) {
  if (guide.axis === "x") {
    return {
      left: `${trackLinePercent(tracks.columns, guide.line, () => columnLinePercent(guide.line, systemColumnVisible))}%`,
      top: `${trackLinePercent(tracks.rows, guide.segmentStart, () => rowLinePercent(guide.segmentStart))}%`,
      height: `${trackSpanPercent(tracks.rows, guide.segmentStart, guide.segmentSpan, () => (guide.segmentSpan / workspaceRowCount) * 100)}%`
    };
  }

  return {
    left: `${trackLinePercent(tracks.columns, guide.segmentStart, () => columnLinePercent(guide.segmentStart, systemColumnVisible))}%`,
    top: `${trackLinePercent(tracks.rows, guide.line, () => rowLinePercent(guide.line))}%`,
    width: `${trackSpanPercent(tracks.columns, guide.segmentStart, guide.segmentSpan, () => columnSpanPercent(guide.segmentStart, guide.segmentSpan, systemColumnVisible))}%`
  };
}

function trackPositions(tracks: number[]): number[] {
  const positions = [0];
  let nextPosition = 0;

  for (const track of tracks) {
    nextPosition += track;
    positions.push(nextPosition);
  }

  return positions;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function parseGridTracks(value: string): number[] {
  return value
    .split(" ")
    .map((track) => Number.parseFloat(track))
    .filter((track) => Number.isFinite(track) && track > 0);
}

function setTrackVariables(frame: HTMLElement, axis: BoundaryResizeGuide["axis"], tracks: number[]) {
  tracks.forEach((track, index) => {
    frame.style.setProperty(`--frame-${axis === "x" ? "col" : "row"}-${index + 1}`, `${track}px`);
  });
}

function clearTrackVariables(frame: HTMLElement) {
  for (let index = 1; index <= workspaceColumnCount + 1; index += 1) {
    frame.style.removeProperty(`--frame-col-${index}`);
  }

  for (let index = 1; index <= workspaceRowCount; index += 1) {
    frame.style.removeProperty(`--frame-row-${index}`);
  }
}

function continuousGuideOffset(
  guide: BoundaryResizeGuide,
  baseTracks: number[],
  startX: number,
  startY: number,
  latestX: number,
  latestY: number
): number {
  const rawDelta = guide.axis === "x" ? latestX - startX : latestY - startY;
  const lines = trackPositions(baseTracks);
  const startLinePosition = lines[guide.line - 1];
  const min = guide.canDecrease ? lines[0] - startLinePosition : 0;
  const max = guide.canIncrease ? lines[lines.length - 1] - startLinePosition : 0;
  return clamp(rawDelta, min, max);
}

function resizeTracks(
  guide: BoundaryResizeGuide,
  baseTracks: number[],
  offset: number
): number[] {
  const beforeIndex = guide.line - 2;
  const afterIndex = guide.line - 1;
  const before = baseTracks[beforeIndex];
  const after = baseTracks[afterIndex];

  if (before === undefined || after === undefined) {
    return baseTracks;
  }

  const minTrackSize = guide.axis === "x" ? 96 : 72;
  const minOffset = guide.canDecrease ? minTrackSize - before : 0;
  const maxOffset = guide.canIncrease ? after - minTrackSize : 0;
  const clampedOffset = clamp(offset, minOffset, maxOffset);

  return baseTracks.map((track, index) => {
    if (index === beforeIndex) {
      return before + clampedOffset;
    }

    if (index === afterIndex) {
      return after - clampedOffset;
    }

    return track;
  });
}

export function BoundaryResizeOverlay({ layout, onPreviewChange, onTrackResize, systemColumnVisible, tracks }: BoundaryResizeOverlayProps) {
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
    target.setPointerCapture?.(event.pointerId);

    const startX = event.clientX;
    const startY = event.clientY;
    let latestX = startX;
    let latestY = startY;
    let finished = false;
    let moved = false;
    let latestTracks: number[] = [];
    let resizeFrame: number | null = null;
    const computedFrameStyle = getComputedStyle(frame);
    const baseTracks = parseGridTracks(guide.axis === "x" ? computedFrameStyle.gridTemplateColumns : computedFrameStyle.gridTemplateRows);
    latestTracks = baseTracks;
    setTrackVariables(frame, guide.axis, baseTracks);
    frame.classList.add("resizing-grid");
    onPreviewChange([]);

    const scheduleTrackResize = () => {
      if (resizeFrame !== null) {
        return;
      }

      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = null;
        onTrackResize(guide.axis, latestTracks);
      });
    };

    const handlePointerMove = (moveEvent: PointerEvent) => {
      latestX = moveEvent.clientX;
      latestY = moveEvent.clientY;
      const offset = continuousGuideOffset(guide, baseTracks, startX, startY, latestX, latestY);
      latestTracks = resizeTracks(guide, baseTracks, offset);
      moved = moved || Math.abs(offset) > 0.5;
      setTrackVariables(frame, guide.axis, latestTracks);
      scheduleTrackResize();
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
      if (resizeFrame !== null) {
        window.cancelAnimationFrame(resizeFrame);
        resizeFrame = null;
      }
      frame.classList.remove("resizing-grid");
      onPreviewChange([]);

      if (!moved) {
        clearTrackVariables(frame);
        return;
      }

      setTrackVariables(frame, guide.axis, latestTracks);
      onTrackResize(guide.axis, latestTracks);
    };

    target.addEventListener("pointermove", handlePointerMove);
    target.addEventListener("pointerup", handlePointerUp, { once: true });
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp, { once: true });
  };

  return (
    <div className="boundary-overlay" aria-label="패널 경계 조정">
      {guides.map((guide) => (
        <div key={guide.id} className={`boundary-guide-slot ${guide.axis === "x" ? "vertical" : "horizontal"}`} style={guideStyle(guide, tracks, systemColumnVisible)}>
          <button
            type="button"
            className={`boundary-guide ${guide.axis === "x" ? "vertical" : "horizontal"}`}
            title="패널 경계 조정"
            onPointerDown={(event) => beginDrag(guide, event)}
          >
            <span />
          </button>
        </div>
      ))}
    </div>
  );
}
