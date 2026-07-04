import type { CSSProperties } from "react";
import type { PanelBoundary, PanelSlot, PanelSlotId, TiledPanelState, ViewportSize } from "../layout/panelLayout";
import { bottomNavigationHeight } from "../layout/workspaceMetrics";

export const panelNavHeight = 30;

export function boundaryStyle(boundary: PanelBoundary): CSSProperties {
  if (boundary.orientation === "vertical") {
    return {
      left: boundary.position - 7,
      top: boundary.rangeStart,
      width: 14,
      height: boundary.rangeEnd - boundary.rangeStart
    };
  }
  return {
    left: boundary.rangeStart,
    top: boundary.position - 7,
    width: boundary.rangeEnd - boundary.rangeStart,
    height: 14
  };
}

export function boundaryAddMenuPosition(
  boundary: PanelBoundary,
  optionCount: number,
  viewport: ViewportSize,
  gutter: number
): { left: number; top: number } {
  const menuWidth = 126;
  const menuHeight = optionCount * 28 + 10;
  const desiredLeft = boundary.orientation === "vertical"
    ? boundary.position
    : (boundary.rangeStart + boundary.rangeEnd) / 2;
  const desiredTop = boundary.orientation === "vertical"
    ? (boundary.rangeStart + boundary.rangeEnd) / 2
    : boundary.position;
  const minLeft = gutter + menuWidth / 2;
  const maxLeft = viewport.width - gutter - menuWidth / 2;
  const minTop = gutter + menuHeight / 2;
  const maxTop = viewport.height - bottomNavigationHeight - gutter - menuHeight / 2;

  return {
    left: clampNumber(desiredLeft, minLeft, maxLeft),
    top: clampNumber(desiredTop, minTop, maxTop)
  };
}

export function hitTestSwappableSlot(
  state: TiledPanelState,
  x: number,
  y: number,
  sourceSlotId: PanelSlotId
): PanelSlot | null {
  return state.slots.find((slot) => (
    slot.id !== sourceSlotId &&
    !slot.required &&
    x >= slot.rect.left &&
    x <= slot.rect.left + slot.rect.width &&
    y >= slot.rect.top &&
    y <= slot.rect.top + slot.rect.height
  )) ?? null;
}

function clampNumber(value: number, min: number, max: number): number {
  if (max < min) {
    return (min + max) / 2;
  }
  return Math.min(max, Math.max(min, value));
}
