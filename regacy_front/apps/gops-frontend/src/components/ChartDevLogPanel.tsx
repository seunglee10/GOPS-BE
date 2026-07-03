import type { ChartRuntimeState } from "@gops/chart-engine/runtime";
import { chartDevLogHighlights, type ChartDevLogEntry } from "../diagnostics/chartDevLog";

type ChartDevLogPanelProps = {
  entries: readonly ChartDevLogEntry[];
  chartRuntime: ChartRuntimeState;
};

const DETAIL_LIMIT = 420;

// DEV-ONLY: 차트/backfill 개발 완료 후 이 패널은 레이아웃과 함께 제거한다.
export function ChartDevLogPanel({ entries, chartRuntime }: ChartDevLogPanelProps) {
  const documentCount = Object.keys(chartRuntime.documents).length;
  const candleStoreCount = Object.keys(chartRuntime.candlesByKey).length;
  const errorCount = entries.filter((entry) => entry.level === "error").length + chartRuntime.errors.length;

  return (
    <div className="chart-dev-log-panel">
      <div className="chart-dev-log-summary" aria-label="chart developer diagnostics summary">
        <span>
          <strong>{documentCount}</strong>
          <em>Docs</em>
        </span>
        <span>
          <strong>{candleStoreCount}</strong>
          <em>Stores</em>
        </span>
        <span>
          <strong>{errorCount}</strong>
          <em>Errors</em>
        </span>
      </div>
      <div className="chart-dev-log-list" role="log" aria-live="polite">
        {entries.length === 0 ? (
          <div className="chart-dev-log-empty">아직 차트 진단 로그가 없습니다</div>
        ) : (
          entries.map((entry) => <ChartDevLogRow key={entry.id} entry={entry} />)
        )}
      </div>
    </div>
  );
}

function ChartDevLogRow({ entry }: { entry: ChartDevLogEntry }) {
  const context = [
    entry.symbol,
    entry.interval,
    entry.panelId ? `panel:${entry.panelId}` : undefined,
    entry.chartDocumentId ? `doc:${entry.chartDocumentId}` : undefined
  ].filter(Boolean).join(" / ");
  const details = formatDetails(entry.details);
  const highlights = chartDevLogHighlights(entry);

  return (
    <div className={`chart-dev-log-row ${entry.level}`}>
      <div className="chart-dev-log-row-head">
        <time>{formatTime(entry.createdAt)}</time>
        <span>{entry.level}</span>
        <em>{entry.category}</em>
      </div>
      <strong>{entry.message}</strong>
      {context && <small>{context}</small>}
      {highlights.length > 0 && (
        <div className="chart-dev-log-highlights" aria-label="chart diagnostic highlights">
          {highlights.map((highlight) => (
            <span
              key={`${highlight.label}:${highlight.value}`}
              className={highlight.tone ? `chart-dev-log-highlight ${highlight.tone}` : "chart-dev-log-highlight"}
            >
              <b>{highlight.label}</b>
              <em>{highlight.value}</em>
            </span>
          ))}
        </div>
      )}
      {details && <code>{details}</code>}
    </div>
  );
}

function formatTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleTimeString("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false
  });
}

function formatDetails(details?: Record<string, unknown>): string {
  if (!details || Object.keys(details).length === 0) {
    return "";
  }

  const formatted = JSON.stringify(details, (_key, value) => {
    if (value instanceof Error) {
      return { name: value.name, message: value.message };
    }
    return value;
  });

  return formatted.length > DETAIL_LIMIT ? `${formatted.slice(0, DETAIL_LIMIT)}...` : formatted;
}
