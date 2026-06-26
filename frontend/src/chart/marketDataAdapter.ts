import type { CandleData, CandleEvent, CandleEventType, CandleSnapshot } from "./types";

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function isSyntheticMarketPayload(source: Record<string, unknown>): boolean {
  const payloadSource = readString(source.source)?.toLowerCase() ?? "";
  const feed = readString(source.feed)?.toLowerCase() ?? "";
  return readBoolean(source.isSynthetic) === true || payloadSource.includes("dummy") || payloadSource.includes("demo") || feed.includes("synthetic") || feed.includes("demo");
}

function normalizeCandle(value: unknown): CandleData | null {
  if (!value || typeof value !== "object") {
    return null;
  }

  const source = value as Record<string, unknown>;
  const timestamp = readString(source.timestamp);
  const open = readNumber(source.open);
  const high = readNumber(source.high);
  const low = readNumber(source.low);
  const close = readNumber(source.close);
  const volume = readNumber(source.volume);
  const ma5 = readNumber(source.ma5);
  const ma20 = readNumber(source.ma20);
  const ma60 = readNumber(source.ma60);

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
  const interval = readString(source.interval);
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
    isSynthetic: isSyntheticMarketPayload(source),
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
  const interval = readString(source.interval);
  const candle = normalizeCandle(source.data);

  if (!type || !symbol || !interval || !candle) {
    throw new Error("Candle event is missing type, symbol, interval, or data.");
  }

  return {
    type,
    symbol,
    interval,
    source: readString(source.source) ?? undefined,
    feed: readString(source.feed) ?? undefined,
    isSynthetic: isSyntheticMarketPayload(source),
    data: candle
  };
}
