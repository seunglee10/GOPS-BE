import { normalizeChartInterval, type ChartInterval } from "./intervals";
import type { CandleData } from "./types";

const marketTimeZone = "America/New_York";
const monthNames = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export type TimeAxisBoundaryKind = "day" | "month" | "year";

export type TimeAxisMarker = {
  visibleIndex: number;
  timestamp: string;
  label: string;
};

export type TimeAxisBoundary = TimeAxisMarker & {
  kind: TimeAxisBoundaryKind;
};

export type TimeAxisLayout = {
  labels: TimeAxisMarker[];
  boundaries: TimeAxisBoundary[];
};

export type TimeAxisLayoutOptions = {
  visibleSlotCount?: number;
  slotWidth?: number;
};

export function canonicalTimestamp(value: string): string | null {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) {
    return null;
  }
  return new Date(parsed).toISOString().replace(/\.\d{3}Z$/, ".000Z");
}

export function buildTimeAxisLayout(
  candles: CandleData[],
  timeframe: string,
  plotWidth: number,
  options: TimeAxisLayoutOptions = {}
): TimeAxisLayout {
  if (candles.length === 0) {
    return { labels: [], boundaries: [] };
  }

  const interval = normalizeChartInterval(timeframe) ?? "1m";
  const slotWidth = options.slotWidth ?? Math.max(1, plotWidth) / Math.max(1, options.visibleSlotCount ?? candles.length);
  const boundaries = buildTimeAxisBoundaries(candles, interval);
  const maxLabelCount = Math.max(2, Math.floor(Math.max(1, plotWidth) / minLabelSpacingPx(interval)));
  const minIndexGap = Math.max(1, Math.ceil(minLabelSpacingPx(interval) / Math.max(1, slotWidth)));
  const candidates: Array<TimeAxisMarker & { priority: number }> = [
    ...boundaries.map((boundary) => ({ ...boundary, priority: 4 })),
    { visibleIndex: 0, timestamp: candles[0].timestamp, label: formatAxisTickLabel(candles[0].timestamp, interval), priority: 2 },
    {
      visibleIndex: candles.length - 1,
      timestamp: candles[candles.length - 1].timestamp,
      label: formatAxisTickLabel(candles[candles.length - 1].timestamp, interval),
      priority: 2
    }
  ];

  const sampleStep = Math.max(1, Math.ceil(candles.length / maxLabelCount));
  for (let index = sampleStep; index < candles.length - 1; index += sampleStep) {
    candidates.push({
      visibleIndex: index,
      timestamp: candles[index].timestamp,
      label: formatAxisTickLabel(candles[index].timestamp, interval),
      priority: 1
    });
  }

  const selected: Array<TimeAxisMarker & { priority: number }> = [];
  for (const candidate of candidates.sort(compareAxisCandidate)) {
    if (selected.some((existing) => Math.abs(existing.visibleIndex - candidate.visibleIndex) < minIndexGap)) {
      continue;
    }
    selected.push(candidate);
  }

  return {
    boundaries,
    labels: selected
      .sort((left, right) => left.visibleIndex - right.visibleIndex)
      .map(({ priority: _priority, ...marker }) => marker)
  };
}

export function formatAxisTickLabel(timestamp: string, timeframe: string): string {
  const interval = normalizeChartInterval(timeframe) ?? "1m";
  const parts = calendarParts(timestamp, interval);
  if (!parts) {
    return "";
  }
  if (isIntradayInterval(interval)) {
    return `${two(parts.hour)}:${two(parts.minute)}`;
  }
  if (interval === "1M") {
    return `${monthNames[parts.month - 1]} ${parts.year}`;
  }
  return `${monthNames[parts.month - 1]} ${parts.day}`;
}

export function formatCrosshairTimestamp(timestamp: string, timeframe: string): string {
  const interval = normalizeChartInterval(timeframe) ?? "1m";
  const parts = calendarParts(timestamp, interval);
  if (!parts) {
    return timestamp;
  }
  if (isIntradayInterval(interval)) {
    return `${monthNames[parts.month - 1]} ${parts.day} ${two(parts.hour)}:${two(parts.minute)}`;
  }
  if (interval === "1W") {
    return `Week of ${monthNames[parts.month - 1]} ${parts.day}, ${parts.year}`;
  }
  if (interval === "1M") {
    return `${monthNames[parts.month - 1]} ${parts.year}`;
  }
  return `${monthNames[parts.month - 1]} ${parts.day}, ${parts.year}`;
}

function buildTimeAxisBoundaries(candles: CandleData[], interval: ChartInterval): TimeAxisBoundary[] {
  const boundaries: TimeAxisBoundary[] = [];
  for (let index = 1; index < candles.length; index += 1) {
    const previous = calendarParts(candles[index - 1].timestamp, interval);
    const current = calendarParts(candles[index].timestamp, interval);
    if (!previous || !current) {
      continue;
    }
    const kind = boundaryKind(previous, current, interval);
    if (!kind) {
      continue;
    }
    boundaries.push({
      visibleIndex: index,
      timestamp: candles[index].timestamp,
      kind,
      label: formatBoundaryLabel(current, kind, interval)
    });
  }
  return boundaries;
}

function boundaryKind(previous: CalendarParts, current: CalendarParts, interval: ChartInterval): TimeAxisBoundaryKind | null {
  if (isIntradayInterval(interval) && calendarDayKey(current) !== calendarDayKey(previous)) {
    return "day";
  }
  if (interval === "1D" && calendarMonthKey(current) !== calendarMonthKey(previous)) {
    return "month";
  }
  if (interval === "1W" && current.year !== previous.year) {
    return "year";
  }
  if (interval === "1M" && current.year !== previous.year) {
    return "year";
  }
  return null;
}

function formatBoundaryLabel(parts: CalendarParts, kind: TimeAxisBoundaryKind, interval: ChartInterval): string {
  if (kind === "year") {
    return String(parts.year);
  }
  if (kind === "month" || interval === "1M") {
    return `${monthNames[parts.month - 1]} ${parts.year}`;
  }
  return `${monthNames[parts.month - 1]} ${parts.day}`;
}

function compareAxisCandidate(
  left: TimeAxisMarker & { priority: number },
  right: TimeAxisMarker & { priority: number }
): number {
  if (right.priority !== left.priority) {
    return right.priority - left.priority;
  }
  return left.visibleIndex - right.visibleIndex;
}

function minLabelSpacingPx(interval: ChartInterval): number {
  if (interval === "1M") {
    return 78;
  }
  if (interval === "1W" || interval === "1D") {
    return 64;
  }
  return 58;
}

function isIntradayInterval(interval: ChartInterval): boolean {
  return interval === "1m" || interval === "5m" || interval === "10m";
}

function calendarDayKey(parts: CalendarParts): string {
  return `${parts.year}-${two(parts.month)}-${two(parts.day)}`;
}

function calendarMonthKey(parts: CalendarParts): string {
  return `${parts.year}-${two(parts.month)}`;
}

type CalendarParts = {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
};

function calendarParts(timestamp: string, interval: ChartInterval): CalendarParts | null {
  const date = new Date(timestamp);
  if (!Number.isFinite(date.getTime())) {
    return null;
  }
  if (isIntradayInterval(interval)) {
    return zonedCalendarParts(date, marketTimeZone);
  }

  // Daily, weekly, and monthly candles are canonical date buckets, not trade instants.
  return marketDateBucketParts(timestamp) ?? zonedCalendarParts(date, marketTimeZone);
}

function marketDateBucketParts(timestamp: string): CalendarParts | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(timestamp);
  if (!match) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const normalized = new Date(Date.UTC(year, month - 1, day));
  if (
    !Number.isFinite(normalized.getTime()) ||
    normalized.getUTCFullYear() !== year ||
    normalized.getUTCMonth() + 1 !== month ||
    normalized.getUTCDate() !== day
  ) {
    return null;
  }
  return { year, month, day, hour: 0, minute: 0 };
}

function zonedCalendarParts(date: Date, timeZone: string): CalendarParts | null {
  const values: Record<string, number> = {};
  const formatter = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23"
  });
  for (const part of formatter.formatToParts(date)) {
    if (part.type === "year" || part.type === "month" || part.type === "day" || part.type === "hour" || part.type === "minute") {
      values[part.type] = Number(part.value);
    }
  }
  if (!values.year || !values.month || !values.day || !Number.isFinite(values.hour) || !Number.isFinite(values.minute)) {
    return null;
  }
  return {
    year: values.year,
    month: values.month,
    day: values.day,
    hour: values.hour === 24 ? 0 : values.hour,
    minute: values.minute
  };
}

function two(value: number): string {
  return String(value).padStart(2, "0");
}
