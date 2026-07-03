import { panelTypes } from "./panelRegistry";
import type { DefaultLayoutKey, FavoriteLayoutSlot, PanelType, SavedLayoutRecord, SavedLayoutKind, WorkspaceLayout } from "./types";

const STORAGE_KEY = "gops.savedLayouts.v1";
const MAX_SAVED_LAYOUT_RECORDS = 8;

function isWorkspaceLayout(value: unknown): value is WorkspaceLayout {
  if (!value || typeof value !== "object") {
    return false;
  }

  const layout = value as WorkspaceLayout;
  return (
    layout.version === 1 &&
    Boolean(layout.zones?.workspace) &&
    Boolean(layout.zones?.agentRail) &&
    Array.isArray(layout.panels) &&
    Boolean(layout.settings)
  );
}

export function loadSavedLayouts(): { records: SavedLayoutRecord[]; error?: string } {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      return { records: [] };
    }

    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) {
      return { records: [], error: "Saved layout store is invalid." };
    }

    let ignoredRecordCount = 0;
    let filteredPanelCount = 0;
    const records = parsed.flatMap((item): SavedLayoutRecord[] => {
      const record = item as SavedLayoutRecord;
      if (record.version !== 1 || typeof record.id !== "string" || !isWorkspaceLayout(record.layout)) {
        ignoredRecordCount += 1;
        return [];
      }

      const sanitizedLayout = sanitizeWorkspaceLayout(record.layout);
      filteredPanelCount += record.layout.panels.length - (sanitizedLayout?.panels.length ?? 0);
      if (!sanitizedLayout) {
        ignoredRecordCount += 1;
        return [];
      }

      return [{
        ...record,
        kind: isSavedLayoutKind(record.kind) ? record.kind : "user",
        defaultKey: isDefaultLayoutKey(record.defaultKey) ? record.defaultKey : undefined,
        favoriteSlot: isFavoriteSlot(record.favoriteSlot) ? record.favoriteSlot : undefined,
        layout: sanitizedLayout
      }];
    }).slice(0, MAX_SAVED_LAYOUT_RECORDS);

    if (ignoredRecordCount > 0 || filteredPanelCount > 0 || records.length !== parsed.length) {
      return { records, error: "Some invalid or retired saved layout panels were ignored." };
    }

    return { records };
  } catch {
    return { records: [], error: "Could not read saved layouts." };
  }
}

function isKnownPanelType(value: unknown): value is PanelType {
  return typeof value === "string" && panelTypes.includes(value as PanelType);
}

function sanitizeWorkspaceLayout(layout: WorkspaceLayout): WorkspaceLayout | null {
  const panels = layout.panels.filter((panel) => isKnownPanelType(panel.type));
  if (panels.length === 0) {
    return null;
  }

  return {
    ...layout,
    panels,
    selectedPanelId: panels.some((panel) => panel.id === layout.selectedPanelId) ? layout.selectedPanelId : undefined
  };
}

function isFavoriteSlot(value: unknown): value is FavoriteLayoutSlot {
  return value === 1 || value === 2 || value === 3 || value === 4;
}

function isSavedLayoutKind(value: unknown): value is SavedLayoutKind {
  return value === "default" || value === "user";
}

function isDefaultLayoutKey(value: unknown): value is DefaultLayoutKey {
  return value === "chart" || value === "news" || value === "overview" || value === "signals";
}

export function persistSavedLayouts(records: SavedLayoutRecord[]): string | null {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(records));
    return null;
  } catch {
    return "Could not persist saved layouts.";
  }
}

export function createSavedLayoutRecord(name: string, layout: WorkspaceLayout): SavedLayoutRecord {
  return {
    id: `saved-${crypto.randomUUID()}`,
    name,
    version: 1,
    savedAt: new Date().toISOString(),
    kind: "user",
    layout: structuredClone(layout) as WorkspaceLayout
  };
}
