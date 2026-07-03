import { normalizeChartInterval } from "./intervals";
import { canonicalTimestamp } from "./time";
import type { BackfillStatus, CandleData, CandleEvent, CandleEventType, CandleSnapshot, ChartCoverage, ChartCoverageState, ChartRequestedRange, ChartSnapshotDataStatus, RepairStatus } from "./types";

export type RealtimeControlType = "HEARTBEAT" | "MARKET_STATUS_UPDATE" | "VOLUME_PROFILE_BINS_UPDATE" | "ERROR";

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function normalizeCandle(value: unknown): CandleData | null {
  if (!value || typeof value !== "object") {
    return null;
  }

  const source = value as Record<string, unknown>;
  const timestamp = canonicalTimestamp(readString(source.timestamp) ?? "");
  const open = readNumber(source.open);
  const high = readNumber(source.high);
  const low = readNumber(source.low);
  const close = readNumber(source.close);
  const volume = readNumber(source.volume);
  const ma5 = readNumber(source.ma5);
  const ma20 = readNumber(source.ma20);
  const ma60 = readNumber(source.ma60);
  const sourceInterval = readString(source.sourceInterval);
  const feedProfile = readString(source.feedProfile);
  const marketSession = readString(source.marketSession);
  const updatedAt = readString(source.updatedAt);

  if (!timestamp || open === null || high === null || low === null || close === null || volume === null) {
    return null;
  }

  return {
    timestamp,
    open,
    high,
    low,
    close,
    volume,
    isClosed: typeof source.isClosed === "boolean" ? source.isClosed : true,
    ...(sourceInterval ? { sourceInterval } : {}),
    ...(feedProfile ? { feedProfile } : {}),
    ...(marketSession ? { marketSession } : {}),
    ...(updatedAt ? { updatedAt } : {}),
    ...(ma5 !== null ? { ma5 } : {}),
    ...(ma20 !== null ? { ma20 } : {}),
    ...(ma60 !== null ? { ma60 } : {})
  };
}

function readCandleEventType(value: unknown): CandleEventType | null {
  return value === "LIVE_CANDLE_UPDATE" || value === "CANDLE_CLOSED" || value === "CANDLE_CORRECTED"
    ? value
    : null;
}

function readDataStatus(value: unknown): ChartSnapshotDataStatus | undefined {
  return value === "ready" || value === "partial" || value === "empty" || value === "error" ? value : undefined;
}

function readBackfillStatus(value: unknown): BackfillStatus | undefined {
  return value === "not_requested" ||
    value === "queued" ||
    value === "running" ||
    value === "succeeded" ||
    value === "failed" ||
    value === "unavailable"
    ? value
    : undefined;
}

function readRepairStatus(value: unknown): RepairStatus | undefined {
  return value === "none" ||
    value === "gapfill_required" ||
    value === "gapfill_active" ||
    value === "gapfill_failed"
    ? value
    : undefined;
}

function readCoverageState(value: unknown): ChartCoverageState | undefined {
  return value === "complete" || value === "partial" || value === "empty" || value === "unavailable" ? value : undefined;
}

function normalizeCoverage(value: unknown): ChartCoverage | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  const source = value as Record<string, unknown>;
  const state = readCoverageState(source.state);
  if (!state) {
    return undefined;
  }
  return {
    state,
    reasonCode: readString(source.reasonCode) ?? undefined,
    message: readString(source.message) ?? undefined,
    repairStatus: readRepairStatus(source.repairStatus),
    sourceInterval: readString(source.sourceInterval) ?? undefined,
    backfillStatus: readBackfillStatus(source.backfillStatus),
    requestedLimit: readNumber(source.requestedLimit) ?? undefined,
    returnedCount: readNumber(source.returnedCount) ?? undefined,
    storedCandleCount: readNumber(source.storedCandleCount) ?? undefined,
    availableFrom: readString(source.availableFrom) ?? undefined,
    availableTo: readString(source.availableTo) ?? undefined,
    noDataBefore: readString(source.noDataBefore) ?? undefined,
    requestedRange: normalizeRequestedRange(source.requestedRange),
    invalidRowCount: readNumber(source.invalidRowCount) ?? undefined,
    renderable: readBoolean(source.renderable) ?? undefined,
    minimumReturnedCount: readNumber(source.minimumReturnedCount) ?? undefined,
    minimumRenderableSourceBars: readNumber(source.minimumRenderableSourceBars) ?? undefined,
    expectedRequestedRangeBars: readNumber(source.expectedRequestedRangeBars) ?? undefined,
    returnedSpanSeconds: readNumber(source.returnedSpanSeconds) ?? undefined,
    maxRenderableSpanSeconds: readNumber(source.maxRenderableSpanSeconds) ?? undefined,
    renderabilityReasonCode: readString(source.renderabilityReasonCode) ?? undefined
  };
}

function normalizeRequestedRange(value: unknown): ChartRequestedRange | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  const source = value as Record<string, unknown>;
  const range: ChartRequestedRange = {};
  const before = readString(source.before);
  const from = readString(source.from);
  const to = readString(source.to);
  if (before) {
    range.before = before;
  }
  if (from) {
    range.from = from;
  }
  if (to) {
    range.to = to;
  }
  return Object.keys(range).length ? range : undefined;
}

export function isRealtimeControlPayload(payload: unknown): payload is Record<string, unknown> & { type: RealtimeControlType } {
  if (!payload || typeof payload !== "object") {
    return false;
  }
  const type = (payload as Record<string, unknown>).type;
  return type === "HEARTBEAT" || type === "MARKET_STATUS_UPDATE" || type === "VOLUME_PROFILE_BINS_UPDATE" || type === "ERROR";
}

function normalizeIndicators(value: unknown): CandleSnapshot["indicators"] {
  if (!value || typeof value !== "object") {
    return { ma: [5, 20, 60], volume: true };
  }

  const source = value as Record<string, unknown>;
  const ma = Array.isArray(source.ma)
    ? source.ma.filter((item): item is number => typeof item === "number" && Number.isFinite(item))
    : [5, 20, 60];

  return {
    ma: ma.length ? ma : [5, 20, 60],
    volume: typeof source.volume === "boolean" ? source.volume : true
  };
}

export function normalizeCandleSnapshot(payload: unknown): CandleSnapshot {
  if (!payload || typeof payload !== "object") {
    throw new Error("Candle snapshot payload is invalid.");
  }

  const source = payload as Record<string, unknown>;
  const symbol = readString(source.symbol);
  const interval = normalizeChartInterval(readString(source.interval) ?? "");
  const candles = Array.isArray(source.candles)
    ? source.candles.map(normalizeCandle).filter((item): item is CandleData => Boolean(item))
    : [];

  if (!symbol || !interval) {
    throw new Error("Candle snapshot is missing symbol or interval.");
  }

  return {
    symbol,
    interval,
    source: readString(source.source) ?? "unknown",
    feed: readString(source.feed) ?? "unknown",
    feedProfile: readString(source.feedProfile) ?? undefined,
    marketSession: readString(source.marketSession) ?? undefined,
    snapshotCursor: readString(source.snapshotCursor) ?? undefined,
    dataStatus: readDataStatus(source.dataStatus),
    backfillStatus: readBackfillStatus(source.backfillStatus),
    repairStatus: readRepairStatus(source.repairStatus),
    canBackfill: readBoolean(source.canBackfill) ?? undefined,
    sourceInterval: readString(source.sourceInterval) ?? undefined,
    message: readString(source.message) ?? undefined,
    requestedLimit: readNumber(source.requestedLimit) ?? undefined,
    returnedCount: readNumber(source.returnedCount) ?? undefined,
    storedCandleCount: readNumber(source.storedCandleCount) ?? undefined,
    availableFrom: readString(source.availableFrom) ?? undefined,
    availableTo: readString(source.availableTo) ?? undefined,
    noDataBefore: readString(source.noDataBefore) ?? undefined,
    requestedRange: normalizeRequestedRange(source.requestedRange),
    oldestTimestamp: readString(source.oldestTimestamp) ?? undefined,
    newestTimestamp: readString(source.newestTimestamp) ?? undefined,
    hasMoreBefore: readBoolean(source.hasMoreBefore) ?? undefined,
    hasMoreAfter: readBoolean(source.hasMoreAfter) ?? undefined,
    coverage: normalizeCoverage(source.coverage),
    indicators: normalizeIndicators(source.indicators),
    candles
  };
}

export function normalizeCandleEvent(payload: unknown): CandleEvent {
  if (!payload || typeof payload !== "object") {
    throw new Error("Candle event payload is invalid.");
  }

  const source = payload as Record<string, unknown>;
  const type = readCandleEventType(source.type);
  const symbol = readString(source.symbol);
  const interval = normalizeChartInterval(readString(source.interval) ?? "");
  const sourceInterval = normalizeChartInterval(readString(source.sourceInterval) ?? "");
  const candle = normalizeCandle(source.data);

  if (!type || !symbol || !interval || !candle) {
    throw new Error("Candle event is missing type, symbol, interval, or data.");
  }

  return {
    type,
    eventId: readString(source.eventId) ?? undefined,
    cursor: readString(source.cursor) ?? undefined,
    symbol,
    interval,
    sourceInterval: sourceInterval ?? interval,
    source: readString(source.source) ?? undefined,
    feed: readString(source.feed) ?? undefined,
    feedProfile: readString(source.feedProfile) ?? undefined,
    marketSession: readString(source.marketSession) ?? undefined,
    data: candle
  };
}
