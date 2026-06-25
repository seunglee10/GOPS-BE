import type { ScaleBinding } from "../types/documents";

export interface ScaleDefinition {
  mode: ScaleBinding["mode"];
  label: string;
  defaultPosition: ScaleBinding["position"];
}

export const defaultScaleRegistry: Record<string, ScaleDefinition> = {
  price: { mode: "price", label: "Price", defaultPosition: "right" },
  volume: { mode: "volume", label: "Volume", defaultPosition: "right" },
  percent: { mode: "percent", label: "Percent", defaultPosition: "left" },
  indexedTo100: { mode: "indexedTo100", label: "Indexed to 100", defaultPosition: "left" },
  oscillator: { mode: "oscillator", label: "Oscillator", defaultPosition: "right" }
};
