import type { ThemeColors } from "../theme/colors";

export function tileFillForChange(changePercent: number | undefined, theme: ThemeColors): string {
  const change = Number.isFinite(changePercent) ? Number(changePercent) : 0;
  if (Math.abs(change) < 0.08) {
    return theme.muted;
  }
  return change > 0 ? theme.up : theme.down;
}

export function tileOpacityForChange(changePercent: number | undefined): number {
  const change = Number.isFinite(changePercent) ? Math.abs(Number(changePercent)) : 0;
  if (change < 0.08) {
    return 0.18;
  }
  return Math.min(0.92, 0.28 + change / 6);
}

export function tileTextForChange(changePercent: number | undefined, theme: ThemeColors): string {
  const change = Number.isFinite(changePercent) ? Number(changePercent) : 0;
  return Math.abs(change) >= 1.5 ? theme.tileTextInverse : theme.tileText;
}

export function toneForChange(changePercent: number | undefined): "up" | "down" | "flat" {
  const change = Number.isFinite(changePercent) ? Number(changePercent) : 0;
  if (change > 0.08) {
    return "up";
  }
  if (change < -0.08) {
    return "down";
  }
  return "flat";
}
