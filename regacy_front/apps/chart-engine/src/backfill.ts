import type { BackfillStatus, CandleSnapshot, ChartDataStatus } from "./types";
import {
  defaultVisibleBarsForInterval,
  maxRequestBarsForInterval,
  minimumBackfillSourceBarsForInterval,
  rangeBackfillBufferMultiplierForInterval
} from "./intervals";

export type BackfillStatusPayload = {
  symbol?: string;
  interval?: string;
  sourceInterval?: string;
  requestId?: string;
  status: BackfillStatus;
  error?: string;
  result?: Record<string, unknown>;
};

export type RangeBackfillWindowOptions = {
  bufferMultiplier?: number;
  minimumSourceBars?: number;
};

export type HistoricalRangeRequestKeyInput = {
  symbol: string;
  interval: string;
  before: string;
  pageLimit: number;
  backfillRange?: { start: string; end: string } | null;
};

export type HistoricalRangeLoadPlanInput = {
  symbol: string;
  interval: string;
  candleCount: number;
  oldestTimestamp?: string;
  rightOffset: number;
  visibleCount: number;
  hasMoreBefore?: boolean;
  noDataBefore?: string | null;
};

export type HistoricalRangeLoadPlan = {
  requestKey: string;
  before: string;
  pageLimit: number;
  plannedBackfillRange: { start: string; end: string } | null;
  defaultVisibleCount: number;
  minimumSourceBars: number;
  targetVisibleCount: number;
  visibleEnd: number;
  visibleStart: number;
  bufferedVisibleCount: number;
  bufferMultiplier: number;
  loadedOldestLookaheadCount: number;
  missingVisibleCount: number;
  candleCount: number;
  userZoomedPastDefault: boolean;
  userPannedIntoHistory: boolean;
  isLookingPastLoadedRange: boolean;
  isNearLoadedOldest: boolean;
};

export type HistoricalRangeReadRequest = {
  before?: string;
  from?: string;
  to?: string;
  limit: number;
};

const activeBackfillStatuses = new Set<BackfillStatus>(["queued", "running"]);
const terminalBackfillStatuses = new Set<BackfillStatus>(["succeeded", "failed", "unavailable"]);
const newYorkTimeZone = "America/New_York";
const regularSessionOpenMinute = 9 * 60 + 30;
const regularSessionCloseMinute = 16 * 60;
const regularSessionMinutes = regularSessionCloseMinute - regularSessionOpenMinute;
const newYorkDateTimeFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: newYorkTimeZone,
  hour12: false,
  hourCycle: "h23",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit"
});

export function isActiveBackfillStatus(status?: BackfillStatus): boolean {
  return Boolean(status && activeBackfillStatuses.has(status));
}

export function shouldRequestBackfill(status: ChartDataStatus): boolean {
  const needsSourceData =
    status.state === "empty" ||
    status.state === "error" ||
    status.repairStatus === "gapfill_required" ||
    (status.state === "partial" && status.coverage?.renderable !== true);
  return needsSourceData &&
    status.canBackfill === true &&
    !isActiveBackfillStatus(status.backfillStatus);
}

export function shouldRequestRangeBackfill(snapshot: CandleSnapshot): boolean {
  if (rangeReachedNoDataBoundary(snapshot)) {
    return false;
  }
  const dataState = snapshot.dataStatus ?? (snapshot.candles.length ? "ready" : "empty");
  const needsSourceData =
    dataState === "empty" ||
    dataState === "error" ||
    (dataState === "partial" && snapshot.coverage?.renderable !== true) ||
    snapshot.repairStatus === "gapfill_required";
  return needsSourceData &&
    snapshot.canBackfill === true &&
    !isActiveBackfillStatus(snapshot.backfillStatus);
}

function rangeReachedNoDataBoundary(snapshot: CandleSnapshot): boolean {
  const noDataBefore = Date.parse(snapshot.noDataBefore ?? "");
  if (!Number.isFinite(noDataBefore)) {
    return false;
  }
  const requestedBefore = Date.parse(snapshot.requestedRange?.before ?? "");
  if (Number.isFinite(requestedBefore)) {
    return requestedBefore <= noDataBefore;
  }
  const requestedTo = Date.parse(snapshot.requestedRange?.to ?? "");
  return Number.isFinite(requestedTo) && requestedTo <= noDataBefore;
}

export function rangeBackfillWindow(
  interval: string,
  beforeTimestamp: string,
  visibleBars: number,
  optionsOrBufferMultiplier: RangeBackfillWindowOptions | number = {}
): { start: string; end: string } | null {
  const before = Date.parse(beforeTimestamp);
  if (!Number.isFinite(before)) {
    return null;
  }
  const options = normalizeRangeBackfillWindowOptions(interval, optionsOrBufferMultiplier);
  const units = Math.max(1, Math.floor(visibleBars || 1));
  const bufferedUnits = Math.max(1, options.bufferMultiplier) * units;
  const intradayMinutes = intradayBackfillMinutes(interval, bufferedUnits);
  const start = intradayMinutes === null
    ? new Date(before - intervalBackfillSpanMs(interval, bufferedUnits, options.minimumSourceBars))
    : subtractRegularSessionMinutes(new Date(before), Math.max(intradayMinutes, options.minimumSourceBars));
  return {
    start: start.toISOString(),
    end: new Date(before).toISOString()
  };
}

export function historicalRangeRequestKey(input: HistoricalRangeRequestKeyInput): string {
  return [
    normalizeRequestKeyPart(input.symbol),
    normalizeRequestKeyPart(input.interval),
    "before",
    normalizeRequestKeyPart(input.before),
    "limit",
    Math.max(1, Math.floor(input.pageLimit || 1)),
    "window",
    normalizeRequestKeyPart(input.backfillRange?.start ?? "none"),
    normalizeRequestKeyPart(input.backfillRange?.end ?? "none")
  ].join(":");
}

export function historicalRangeReadRequest(plan: Pick<HistoricalRangeLoadPlan, "before" | "pageLimit" | "plannedBackfillRange">): HistoricalRangeReadRequest {
  const limit = Math.max(1, Math.floor(plan.pageLimit || 1));
  if (plan.plannedBackfillRange) {
    return {
      from: plan.plannedBackfillRange.start,
      to: plan.plannedBackfillRange.end,
      limit
    };
  }
  return {
    before: plan.before,
    limit
  };
}

export function planHistoricalRangeLoad(input: HistoricalRangeLoadPlanInput): HistoricalRangeLoadPlan | null {
  const candleCount = Math.max(0, Math.floor(input.candleCount || 0));
  if (!input.hasMoreBefore || candleCount <= 0 || !input.oldestTimestamp) {
    return null;
  }
  const noDataBoundary = Date.parse(input.noDataBefore ?? "");
  const oldestTime = Date.parse(input.oldestTimestamp);
  if (Number.isFinite(noDataBoundary) && Number.isFinite(oldestTime) && oldestTime <= noDataBoundary) {
    return null;
  }

  const defaultVisibleCount = defaultVisibleBarsForInterval(input.interval);
  const minimumSourceBars = minimumBackfillSourceBarsForInterval(input.interval);
  const targetVisibleCount = Math.max(1, Math.floor(input.visibleCount || 1));
  const rightOffset = Math.max(0, Math.floor(input.rightOffset || 0));
  const visibleEnd = Math.max(0, candleCount - rightOffset);
  const visibleStart = Math.max(0, visibleEnd - targetVisibleCount);
  const userZoomedPastDefault = targetVisibleCount > defaultVisibleCount;
  const userPannedIntoHistory = rightOffset > 0;
  const isLookingPastLoadedRange = targetVisibleCount > candleCount;
  const loadedOldestLookaheadCount = Math.max(24, Math.ceil(targetVisibleCount * 1.25));
  const isNearLoadedOldest =
    userPannedIntoHistory &&
    visibleStart <= loadedOldestLookaheadCount;

  if (!isLookingPastLoadedRange && !isNearLoadedOldest) {
    return null;
  }

  const missingVisibleCount = Math.max(0, targetVisibleCount - candleCount);
  const bufferMultiplier = rangeBackfillBufferMultiplierForInterval(input.interval);
  const bufferedVisibleCount = Math.ceil(targetVisibleCount * bufferMultiplier);
  const pageLimit = Math.min(
    maxRequestBarsForInterval(input.interval),
    Math.max(defaultVisibleCount, bufferedVisibleCount, missingVisibleCount + bufferedVisibleCount)
  );
  let plannedBackfillRange = rangeBackfillWindow(input.interval, input.oldestTimestamp, targetVisibleCount, {
    bufferMultiplier,
    minimumSourceBars
  });
  if (plannedBackfillRange && Number.isFinite(noDataBoundary)) {
    const plannedStart = Date.parse(plannedBackfillRange.start);
    const plannedEnd = Date.parse(plannedBackfillRange.end);
    if (Number.isFinite(plannedEnd) && plannedEnd <= noDataBoundary) {
      return null;
    }
    if (Number.isFinite(plannedStart) && plannedStart < noDataBoundary) {
      plannedBackfillRange = {
        ...plannedBackfillRange,
        start: new Date(noDataBoundary).toISOString()
      };
    }
  }
  const requestKey = historicalRangeRequestKey({
    symbol: input.symbol,
    interval: input.interval,
    before: input.oldestTimestamp,
    pageLimit,
    backfillRange: plannedBackfillRange
  });

  return {
    requestKey,
    before: input.oldestTimestamp,
    pageLimit,
    plannedBackfillRange,
    defaultVisibleCount,
    minimumSourceBars,
    targetVisibleCount,
    visibleEnd,
    visibleStart,
    bufferedVisibleCount,
    bufferMultiplier,
    loadedOldestLookaheadCount,
    missingVisibleCount,
    candleCount,
    userZoomedPastDefault,
    userPannedIntoHistory,
    isLookingPastLoadedRange,
    isNearLoadedOldest
  };
}

export function shouldForceBackfill(status: ChartDataStatus): boolean {
  return Boolean(status.backfillStatus && terminalBackfillStatuses.has(status.backfillStatus));
}

export function isPreparingCandleData(
  status: ChartDataStatus,
  backfillEligible: boolean,
  requestInFlight = false
): boolean {
  return status.state === "empty" &&
    backfillEligible &&
    (
      shouldRequestBackfill(status) ||
      requestInFlight ||
      isActiveBackfillStatus(status.backfillStatus)
    );
}

export function isChartDataRenderable(status: ChartDataStatus): boolean {
  if (status.state === "ready") {
    return true;
  }
  if (status.state !== "partial") {
    return false;
  }
  if (status.coverage?.renderable === true) {
    return true;
  }
  return (status.returnedCount ?? 0) > 0 && (status.coverage?.invalidRowCount ?? 0) <= 0;
}

export function normalizeBackfillStatusPayload(payload: unknown): BackfillStatusPayload {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("Backfill status payload is invalid.");
  }

  const source = payload as Record<string, unknown>;
  const status = readBackfillStatus(source.status);
  if (!status) {
    throw new Error("Backfill status payload is missing status.");
  }

  return {
    symbol: readString(source.symbol) ?? undefined,
    interval: readString(source.interval) ?? undefined,
    sourceInterval: readString(source.sourceInterval) ?? undefined,
    requestId: readString(source.requestId) ?? undefined,
    status,
    error: readString(source.error) ?? undefined,
    result: readRecord(source.result)
  };
}

function readBackfillStatus(value: unknown): BackfillStatus | null {
  return value === "not_requested" ||
    value === "queued" ||
    value === "running" ||
    value === "succeeded" ||
    value === "failed" ||
    value === "unavailable"
    ? value
    : null;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function readRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function normalizeRequestKeyPart(value: string): string {
  return value.trim().replace(/\s+/g, "_");
}

function normalizeRangeBackfillWindowOptions(
  interval: string,
  optionsOrBufferMultiplier: RangeBackfillWindowOptions | number
): Required<RangeBackfillWindowOptions> {
  if (typeof optionsOrBufferMultiplier === "number") {
    return {
      bufferMultiplier: optionsOrBufferMultiplier,
      minimumSourceBars: 0
    };
  }
  return {
    bufferMultiplier: optionsOrBufferMultiplier.bufferMultiplier ?? rangeBackfillBufferMultiplierForInterval(interval),
    minimumSourceBars: Math.max(0, Math.floor(optionsOrBufferMultiplier.minimumSourceBars ?? 0))
  };
}

function intervalBackfillSpanMs(interval: string, units: number, minimumSourceBars = 0): number {
  const minute = 60_000;
  const day = 24 * 60 * minute;
  const minimumCalendarDays = sourceDailyBarsToCalendarDays(minimumSourceBars);
  switch (interval) {
    case "1D":
      return Math.max(units, minimumCalendarDays) * day;
    case "1W":
      return Math.max(units * 7, minimumCalendarDays) * day;
    case "1M":
      return Math.max(units * 31, minimumCalendarDays) * day;
    default:
      return units * minute;
  }
}

function sourceDailyBarsToCalendarDays(sourceBars: number): number {
  if (sourceBars <= 0) {
    return 0;
  }
  return Math.ceil(sourceBars * 7 / 5);
}

function intradayBackfillMinutes(interval: string, units: number): number | null {
  switch (interval) {
    case "1m":
      return units;
    case "5m":
      return units * 5;
    case "10m":
      return units * 10;
    default:
      return null;
  }
}

function subtractRegularSessionMinutes(before: Date, minutes: number): Date {
  let remaining = Math.max(1, Math.floor(minutes));
  let cursor = before;
  let guard = 0;

  while (guard < 5000) {
    guard += 1;
    const local = newYorkParts(cursor);
    if (!isRegularSessionDate(local)) {
      cursor = previousRegularSessionClose(local);
      continue;
    }

    const minuteOfDay = local.hour * 60 + local.minute;
    if (minuteOfDay <= regularSessionOpenMinute) {
      cursor = previousRegularSessionClose(local);
      continue;
    }
    if (minuteOfDay > regularSessionCloseMinute) {
      cursor = zonedNewYorkDate(local.year, local.month, local.day, 16, 0);
      continue;
    }

    const availableToday = minuteOfDay - regularSessionOpenMinute;
    if (availableToday >= remaining) {
      const startMinute = minuteOfDay - remaining;
      return zonedNewYorkDate(
        local.year,
        local.month,
        local.day,
        Math.floor(startMinute / 60),
        startMinute % 60
      );
    }

    remaining -= availableToday;
    cursor = previousRegularSessionClose(local);
  }

  return new Date(before.getTime() - Math.ceil(minutes / regularSessionMinutes) * 24 * 60 * 60_000);
}

type NewYorkParts = {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
};

function newYorkParts(date: Date): NewYorkParts {
  const parts = Object.fromEntries(newYorkDateTimeFormatter.formatToParts(date).map((part) => [part.type, part.value]));
  return {
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: Number(parts.hour),
    minute: Number(parts.minute)
  };
}

function zonedNewYorkDate(year: number, month: number, day: number, hour: number, minute: number): Date {
  const utcGuess = new Date(Date.UTC(year, month - 1, day, hour, minute, 0, 0));
  const actual = newYorkParts(utcGuess);
  const targetUtc = Date.UTC(year, month - 1, day, hour, minute, 0, 0);
  const actualUtc = Date.UTC(actual.year, actual.month - 1, actual.day, actual.hour, actual.minute, 0, 0);
  return new Date(utcGuess.getTime() + targetUtc - actualUtc);
}

function previousRegularSessionClose(local: NewYorkParts): Date {
  let date = new Date(Date.UTC(local.year, local.month - 1, local.day - 1, 12, 0, 0, 0));
  let guard = 0;
  while (guard < 10) {
    guard += 1;
    const parts = {
      year: date.getUTCFullYear(),
      month: date.getUTCMonth() + 1,
      day: date.getUTCDate(),
      hour: 16,
      minute: 0
    };
    if (isRegularSessionDate(parts)) {
      return zonedNewYorkDate(parts.year, parts.month, parts.day, 16, 0);
    }
    date = new Date(Date.UTC(parts.year, parts.month - 1, parts.day - 1, 12, 0, 0, 0));
  }
  return zonedNewYorkDate(local.year, local.month, local.day, 16, 0);
}

function isRegularSessionDate(parts: Pick<NewYorkParts, "year" | "month" | "day">): boolean {
  const day = new Date(Date.UTC(parts.year, parts.month - 1, parts.day)).getUTCDay();
  return day >= 1 && day <= 5 && !isDefaultNyseHoliday(parts);
}

function isDefaultNyseHoliday(parts: Pick<NewYorkParts, "year" | "month" | "day">): boolean {
  const sessionTime = Date.UTC(parts.year, parts.month - 1, parts.day);
  const year = parts.year;
  const holidays = [
    observedFixedHoliday(year, 1, 1),
    nthWeekday(year, 1, 1, 3),
    nthWeekday(year, 2, 1, 3),
    easterSunday(year) - 2 * 24 * 60 * 60_000,
    lastWeekday(year, 5, 1),
    observedFixedHoliday(year, 7, 4),
    nthWeekday(year, 9, 1, 1),
    nthWeekday(year, 11, 4, 4),
    observedFixedHoliday(year, 12, 25),
    observedFixedHoliday(year + 1, 1, 1)
  ];
  if (year >= 2022) {
    holidays.push(observedFixedHoliday(year, 6, 19));
  }
  return holidays.includes(sessionTime);
}

function observedFixedHoliday(year: number, month: number, day: number): number {
  const value = Date.UTC(year, month - 1, day);
  const weekday = new Date(value).getUTCDay();
  if (weekday === 6) {
    return value - 24 * 60 * 60_000;
  }
  if (weekday === 0) {
    return value + 24 * 60 * 60_000;
  }
  return value;
}

function nthWeekday(year: number, month: number, weekday: number, occurrence: number): number {
  const first = Date.UTC(year, month - 1, 1);
  const firstWeekday = new Date(first).getUTCDay();
  const offset = (weekday - firstWeekday + 7) % 7;
  return first + (offset + (occurrence - 1) * 7) * 24 * 60 * 60_000;
}

function lastWeekday(year: number, month: number, weekday: number): number {
  const nextMonth = month === 12 ? Date.UTC(year + 1, 0, 1) : Date.UTC(year, month, 1);
  const lastDay = nextMonth - 24 * 60 * 60_000;
  const lastWeekdayValue = new Date(lastDay).getUTCDay();
  return lastDay - ((lastWeekdayValue - weekday + 7) % 7) * 24 * 60 * 60_000;
}

function easterSunday(year: number): number {
  const a = year % 19;
  const b = Math.floor(year / 100);
  const c = year % 100;
  const d = Math.floor(b / 4);
  const e = b % 4;
  const f = Math.floor((b + 8) / 25);
  const g = Math.floor((b - f + 1) / 3);
  const h = (19 * a + b - d - g + 15) % 30;
  const i = Math.floor(c / 4);
  const k = c % 4;
  const l = (32 + 2 * e + 2 * i - h - k) % 7;
  const m = Math.floor((a + 11 * h + 22 * l) / 451);
  const month = Math.floor((h + l - 7 * m + 114) / 31);
  const day = ((h + l - 7 * m + 114) % 31) + 1;
  return Date.UTC(year, month - 1, day);
}
