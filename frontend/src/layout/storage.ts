import type { DefaultLayoutKey, FavoriteLayoutSlot, SavedLayoutRecord, SavedLayoutKind, WorkspaceLayout } from "./types";

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

    const records = parsed.filter((item): item is SavedLayoutRecord => {
      const record = item as SavedLayoutRecord;
      return record.version === 1 && typeof record.id === "string" && isWorkspaceLayout(record.layout);
    }).map((record) => ({
      ...record,
      kind: isSavedLayoutKind(record.kind) ? record.kind : "user",
      defaultKey: isDefaultLayoutKey(record.defaultKey) ? record.defaultKey : undefined,
      favoriteSlot: isFavoriteSlot(record.favoriteSlot) ? record.favoriteSlot : undefined
    })).slice(0, MAX_SAVED_LAYOUT_RECORDS);

    if (records.length !== parsed.length) {
      return { records, error: "Some invalid saved layouts were ignored." };
    }

    return { records };
  } catch {
    return { records: [], error: "Could not read saved layouts." };
  }
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
