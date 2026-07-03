import type { ChartCoverage } from "@gops/chart-engine/types";

export type ChartDevLogLevel = "debug" | "info" | "warn" | "error";

export type ChartDevLogCategory = "runtime" | "chart-data" | "backfill" | "stream" | "render";

export type ChartDevLogEntry = {
  id: string;
  createdAt: string;
  level: ChartDevLogLevel;
  category: ChartDevLogCategory;
  message: string;
  symbol?: string;
  interval?: string;
  chartDocumentId?: string;
  panelId?: string;
  details?: Record<string, unknown>;
};

export type ChartDevLogInput = Omit<ChartDevLogEntry, "id" | "createdAt">;

export type ChartDevLogHighlight = {
  label: string;
  value: string;
  tone?: ChartDevLogLevel;
};

export const CHART_DEV_LOG_LIMIT = 160;
export const CHART_DEV_LOG_HIGHLIGHT_LIMIT = 10;

let fallbackId = 0;

// DEV-ONLY: 차트/backfill 개발 완료 후 이 로그 모델과 수집 코드는 제거한다.
export function createChartDevLogEntry(input: ChartDevLogInput): ChartDevLogEntry {
  const randomId = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${fallbackId += 1}`;
  return {
    ...input,
    id: `chart-dev-log-${randomId}`,
    createdAt: new Date().toISOString()
  };
}

export function chartDevLogHighlights(entry: Pick<ChartDevLogEntry, "details" | "level">): ChartDevLogHighlight[] {
  const details = entry.details ?? {};
  const highlights: ChartDevLogHighlight[] = [];
  const result = readRecord(details.resultSummary) ?? readRecord(details.result);
  const coverage = readRecord(details.coverageSummary) ?? readRecord(details.coverage);

  addHighlight(highlights, "state", joinedValues([
    readString(details.dataStatus) ?? readString(details.state),
    readString(details.repairStatus) ?? readString(coverage?.repairStatus),
    readString(details.backfillStatus) ?? readString(coverage?.backfillStatus)
  ], "/"), entry.level);

  addHighlight(highlights, "source", readString(details.snapshotSource) ?? readString(details.source) ?? readString(result?.source));
  addHighlight(highlights, "srcInt", readString(details.sourceInterval) ?? readString(coverage?.sourceInterval));
  addHighlight(highlights, "window", formatWindow(details.start, details.end) ?? formatRecordWindow(readRecord(details.plannedBackfillRange)));
  addHighlight(highlights, "before", shortTimestamp(readString(details.before)));
  addHighlight(highlights, "page", formatPage(readNumber(details.pageLimit), readNumber(details.bufferMultiplier)));
  addHighlight(highlights, "candles", formatCandleCounts(details));
  addHighlight(highlights, "coverage", formatCoverage(coverage));
  addHighlight(highlights, "CH", formatAvailableRange(details, coverage));
  addHighlight(highlights, "rows", formatMaterializedRows(result));
  addHighlight(highlights, "ranges", formatRangeCounts(result));
  addHighlight(highlights, "archive", readString(result?.archiveStatus));
  addHighlight(highlights, "need", readBoolean(details.needsBackfill) === true ? "backfill" : readBoolean(details.needsBackfill) === false ? "covered" : undefined);
  addHighlight(highlights, "noDataBefore", shortTimestamp(
    readString(details.noDataBefore) ??
      readString(coverage?.noDataBefore) ??
      readString(result?.noDataBefore)
  ), "warn");

  return highlights.slice(0, CHART_DEV_LOG_HIGHLIGHT_LIMIT);
}

export function summarizeChartCoverage(coverage?: ChartCoverage): Record<string, unknown> | undefined {
  if (!coverage) {
    return undefined;
  }
  return {
    state: coverage.state,
    reasonCode: coverage.reasonCode,
    repairStatus: coverage.repairStatus,
    sourceInterval: coverage.sourceInterval,
    backfillStatus: coverage.backfillStatus,
    returnedCount: coverage.returnedCount,
    storedCandleCount: coverage.storedCandleCount,
    expectedRequestedRangeBars: coverage.expectedRequestedRangeBars,
    renderable: coverage.renderable,
    renderabilityReasonCode: coverage.renderabilityReasonCode,
    availableFrom: coverage.availableFrom,
    availableTo: coverage.availableTo,
    noDataBefore: coverage.noDataBefore,
    invalidRowCount: coverage.invalidRowCount
  };
}

function addHighlight(
  highlights: ChartDevLogHighlight[],
  label: string,
  value: string | undefined,
  tone?: ChartDevLogLevel
) {
  if (!value) {
    return;
  }
  highlights.push({ label, value, tone });
}

function joinedValues(values: Array<string | undefined>, separator: string): string | undefined {
  const compact = values.filter((value): value is string => Boolean(value));
  return compact.length ? compact.join(separator) : undefined;
}

function formatPage(pageLimit: number | undefined, bufferMultiplier: number | undefined): string | undefined {
  if (pageLimit === undefined && bufferMultiplier === undefined) {
    return undefined;
  }
  if (pageLimit !== undefined && bufferMultiplier !== undefined) {
    return `${pageLimit} @ ${bufferMultiplier}x`;
  }
  return pageLimit !== undefined ? String(pageLimit) : `${bufferMultiplier}x`;
}

function formatCandleCounts(details: Record<string, unknown>): string | undefined {
  const returned = readNumber(details.returnedCount) ?? readNumber(details.candleCount);
  const stored = readNumber(details.storedCandleCount);
  if (returned === undefined && stored === undefined) {
    return undefined;
  }
  if (returned !== undefined && stored !== undefined) {
    return `${returned}/${stored}`;
  }
  return String(returned ?? stored);
}

function formatCoverage(coverage?: Record<string, unknown>): string | undefined {
  if (!coverage) {
    return undefined;
  }
  const state = readString(coverage.state);
  const reason = readString(coverage.reasonCode) ?? readString(coverage.renderabilityReasonCode);
  const expected = readNumber(coverage.expectedRequestedRangeBars);
  const returned = readNumber(coverage.returnedCount);
  const parts = [state, reason].filter(Boolean);
  if (returned !== undefined && expected !== undefined) {
    parts.push(`${returned}/${expected}`);
  }
  return parts.length ? parts.join(" ") : undefined;
}

function formatAvailableRange(details: Record<string, unknown>, coverage?: Record<string, unknown>): string | undefined {
  return formatWindow(
    readString(details.availableFrom) ?? readString(coverage?.availableFrom),
    readString(details.availableTo) ?? readString(coverage?.availableTo)
  );
}

function formatMaterializedRows(result?: Record<string, unknown>): string | undefined {
  if (!result) {
    return undefined;
  }
  const raw = readNumber(result.rawRowCount);
  const materialized = readNumber(result.materializedRowCount);
  if (raw === undefined && materialized === undefined) {
    return undefined;
  }
  return `${raw ?? "?"}->${materialized ?? "?"}`;
}

function formatRangeCounts(result?: Record<string, unknown>): string | undefined {
  if (!result) {
    return undefined;
  }
  const gaps = readNumber(result.gapRangeCount);
  const fetches = readNumber(result.fetchRangeCount);
  if (gaps === undefined && fetches === undefined) {
    return undefined;
  }
  return `g${gaps ?? 0}/f${fetches ?? 0}`;
}

function formatRecordWindow(record?: Record<string, unknown>): string | undefined {
  if (!record) {
    return undefined;
  }
  return formatWindow(readString(record.start) ?? readString(record.from), readString(record.end) ?? readString(record.to));
}

function formatWindow(start: unknown, end: unknown): string | undefined {
  const left = shortTimestamp(readString(start));
  const right = shortTimestamp(readString(end));
  if (!left && !right) {
    return undefined;
  }
  return `${left ?? "?"}..${right ?? "?"}`;
}

function shortTimestamp(value?: string): string | undefined {
  if (!value) {
    return undefined;
  }
  const match = value.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
  if (!match) {
    return value;
  }
  if (match[2] === "00:00") {
    return match[1];
  }
  return `${match[1]} ${match[2]}`;
}

function readString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function readNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function readBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function readRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}
