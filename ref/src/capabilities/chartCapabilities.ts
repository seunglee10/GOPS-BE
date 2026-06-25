import manifest from "../../shared/chartCapabilities.json";
import type { Command } from "../types/commands";

export interface ChartCapabilityManifestEntry {
  id: string;
  label: string;
  enabled: boolean;
  commandTypes: string[];
  userEnabled: boolean;
  llmEnabled: boolean;
  notes: string;
}

export interface ChartCapabilitiesManifest {
  version: number;
  capabilities: ChartCapabilityManifestEntry[];
}

export const chartCapabilityManifest = manifest as ChartCapabilitiesManifest;

const KNOWN_COMMAND_TYPES: Command["type"][] = [
  "chart.symbol.set",
  "chart.timeframe.set",
  "chart.viewport.set",
  "chart.indicator.add",
  "chart.indicator.update",
  "chart.indicator.remove",
  "chart.drawing.add",
  "chart.drawing.update",
  "chart.drawing.remove",
  "chart.layer.visibility.set",
  "chart.comparison.add",
  "chart.comparison.remove",
  "panel.pinMode.set",
  "panel.chartTool.set",
  "panel.crosshair.set",
  "proposal.accept",
  "proposal.reject"
];

const knownCommandTypeSet = new Set<string>(KNOWN_COMMAND_TYPES);

export function isKnownCommandType(commandType: string): commandType is Command["type"] {
  return knownCommandTypeSet.has(commandType);
}

export function getEnabledCommandTypes(): Command["type"][] {
  return commandTypesFor((capability) => capability.enabled);
}

export function getUserEnabledCommandTypes(): Command["type"][] {
  return commandTypesFor((capability) => capability.enabled && capability.userEnabled);
}

export function getLlmEnabledCommandTypes(): Command["type"][] {
  return commandTypesFor((capability) => capability.enabled && capability.llmEnabled);
}

export function isCommandTypeEnabled(commandType: string): boolean {
  return getEnabledCommandTypes().includes(commandType as Command["type"]);
}

export function isCommandTypeUserEnabled(commandType: string): boolean {
  return getUserEnabledCommandTypes().includes(commandType as Command["type"]);
}

export function isCommandTypeLlmEnabled(commandType: string): boolean {
  return getLlmEnabledCommandTypes().includes(commandType as Command["type"]);
}

function commandTypesFor(predicate: (capability: ChartCapabilityManifestEntry) => boolean): Command["type"][] {
  const commandTypes = chartCapabilityManifest.capabilities
    .filter(predicate)
    .flatMap((capability) => capability.commandTypes)
    .filter(isKnownCommandType);

  return Array.from(new Set(commandTypes));
}
