import type { RenderBounds } from "./sceneBuilder";
import type { ScaleBinding, ScaleId } from "../types/documents";

export interface ValueAxisTick {
  value: number;
  y: number;
  label: string;
}

export interface ValueScaleModel {
  scaleId: ScaleId;
  mode: ScaleBinding["mode"];
  domain: [number, number];
  range: [number, number];
  ticks: ValueAxisTick[];
  valueToY(value: number): number;
  yToValue(y: number): number;
  formatValue(value: number): string;
}

export interface CreateValueScaleModelArgs {
  scaleId: ScaleId;
  mode: ScaleBinding["mode"];
  domain: [number, number];
  bounds: Pick<RenderBounds, "y" | "height">;
  tickCount?: number;
}

export function createValueScaleModel({
  scaleId,
  mode,
  domain,
  bounds,
  tickCount = 4
}: CreateValueScaleModelArgs): ValueScaleModel {
  const range: [number, number] = [bounds.y + bounds.height - 12, bounds.y + 8];
  const safeDomain = normalizeDomain(domain);
  const model: ValueScaleModel = {
    scaleId,
    mode,
    domain: safeDomain,
    range,
    ticks: [],
    valueToY(value) {
      const [min, max] = safeDomain;
      const [bottom, top] = range;
      if (max === min) return (top + bottom) / 2;
      const ratio = (value - min) / (max - min);
      return bottom - ratio * (bottom - top);
    },
    yToValue(y) {
      const [min, max] = safeDomain;
      const [bottom, top] = range;
      if (bottom === top) return min;
      const ratio = (bottom - y) / (bottom - top);
      return min + ratio * (max - min);
    },
    formatValue(value) {
      return formatValue(value, mode);
    }
  };
  model.ticks = buildValueTicks(model, tickCount);
  return model;
}

export function buildValueTicks(scale: ValueScaleModel, tickCount: number): ValueAxisTick[] {
  const [min, max] = scale.domain;
  const count = Math.max(2, tickCount);
  return Array.from({ length: count }, (_, index) => {
    const value = max - ((max - min) / (count - 1)) * index;
    return {
      value,
      y: scale.valueToY(value),
      label: scale.formatValue(value)
    };
  });
}

function normalizeDomain(domain: [number, number]): [number, number] {
  const min = Number.isFinite(domain[0]) ? domain[0] : 0;
  const max = Number.isFinite(domain[1]) ? domain[1] : 1;
  if (min === max) return [min - 1, max + 1];
  return min < max ? [min, max] : [max, min];
}

function formatValue(value: number, mode: ScaleBinding["mode"]): string {
  if (mode === "percent") return `${value.toFixed(Math.abs(value) < 10 ? 2 : 1)}%`;
  if (mode === "volume") {
    if (Math.abs(value) >= 1000000) return `${(value / 1000000).toFixed(1)}M`;
    if (Math.abs(value) >= 1000) return `${(value / 1000).toFixed(1)}K`;
    return value.toFixed(0);
  }
  if (Math.abs(value) >= 1000000) return `${(value / 1000000).toFixed(1)}M`;
  if (Math.abs(value) >= 1000) return `${(value / 1000).toFixed(1)}K`;
  return value.toFixed(Math.abs(value) < 10 ? 2 : 1);
}
