import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { MarketTicker } from "./components/MarketTicker";
import { TopAppBar } from "./components/TopAppBar";
import { initialAgentOptions, type AgentOption, type AgentUpdatePatch, type SystemMenuTab, type SystemMode } from "./components/SystemArea";
import { WorkspaceGrid } from "./components/WorkspaceGrid";
import {
  DEFAULT_AGENT_DRAFT_SEED,
  isAgentChartReferenceAvailable,
  type AgentChartReference
} from "@gops/chart-engine/agentReference";
import { makeChartCommand } from "@gops/chart-engine/commands";
import {
  chartRuntimeReducer,
  createInitialChartRuntimeState,
  getCandlesForDocument,
  getChartDocumentForPanel,
  type ChartRuntimeAction
} from "@gops/chart-engine/runtime";
import { isRealtimeControlPayload, normalizeCandleEvent } from "@gops/chart-engine/marketDataAdapter";
import { DEFAULT_CHART_SYMBOL, defaultWatchlistSymbols, getSymbolMeta, normalizeHotRankingPayload, normalizeSupportedSymbol, normalizeWatchlistPayload, type HotRankingSymbol, type SupportedSymbol, type WatchlistSymbol } from "@gops/chart-engine/symbols";
import type { CandleEvent } from "@gops/chart-engine/types";
import { CHART_DEV_LOG_LIMIT, createChartDevLogEntry, summarizeChartCoverage, type ChartDevLogEntry, type ChartDevLogInput, type ChartDevLogLevel } from "./diagnostics/chartDevLog";
import {
  applyLayoutProposal,
  createInitialRuntimeState,
  executeCommand,
  makeCommand
} from "./layout/commands";
import { findTargetChartPanel } from "./layout/chartPanelSelection";
import type { LayoutCommand, LayoutProposal, LayoutRuntimeState } from "./layout/types";

type RuntimeAction =
  | { kind: "command"; command: LayoutCommand }
  | { kind: "agentLayoutProposal"; proposal: LayoutProposal };

const WATCHLIST_STORAGE_KEY = "gops.watchlistSymbols.v1";
const QUOTE_WATCHLIST_SYMBOL_LIMIT = 40;
const QUOTE_HOT_SYMBOL_LIMIT = 10;

function mergeSymbolRecords(current: WatchlistSymbol[], incoming: WatchlistSymbol[]): WatchlistSymbol[] {
  const bySymbol = new Map(current.map((item) => [item.symbol, item]));
  for (const item of incoming) {
    bySymbol.set(item.symbol, { ...bySymbol.get(item.symbol), ...item });
  }
  return Array.from(bySymbol.values());
}

function refreshWatchlistRecords(current: WatchlistSymbol[], incoming: WatchlistSymbol[]): WatchlistSymbol[] {
  const incomingBySymbol = new Map(incoming.map((item) => [item.symbol, item]));
  return current.map((item) => ({ ...item, ...incomingBySymbol.get(item.symbol) }));
}

function initialWatchlistSymbols(): WatchlistSymbol[] {
  const stored = readStoredWatchlistSymbols();
  return stored ?? defaultWatchlistSymbols();
}

function readStoredWatchlistSymbols(): WatchlistSymbol[] | null {
  if (typeof window === "undefined") {
    return null;
  }
  const raw = window.localStorage.getItem(WATCHLIST_STORAGE_KEY);
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as unknown;
    const records = Array.isArray(parsed)
      ? parsed.map((item) => typeof item === "string" ? getSymbolMeta(item) : item)
      : [];
    return normalizeWatchlistPayload({ symbols: records });
  } catch {
    return null;
  }
}

function writeStoredWatchlistSymbols(symbols: WatchlistSymbol[]): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(WATCHLIST_STORAGE_KEY, JSON.stringify(symbols.map((item) => item.symbol)));
}

function putWatchlistSymbols(symbols: readonly WatchlistSymbol[]): Promise<unknown | null> {
  return fetch("/api/charts/watchlist", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbols: symbols.map((item) => item.symbol) })
  }).then((response) => response.ok ? response.json() as Promise<unknown> : null);
}

function watchlistRequestUrl(symbols: readonly WatchlistSymbol[]): string {
  const params = new URLSearchParams();
  if (symbols.length) {
    params.set("symbols", symbols.map((item) => item.symbol).join(","));
  }
  const query = params.toString();
  return `/api/charts/watchlist${query ? `?${query}` : ""}`;
}

function applyRealtimeQuote(records: WatchlistSymbol[], event: CandleEvent, volumeDelta: number): WatchlistSymbol[] {
  return records.map((item) => item.symbol === event.symbol ? quoteFromLiveEvent(item, event, volumeDelta) : item);
}

function applyRealtimeHotQuote(records: HotRankingSymbol[], event: CandleEvent, volumeDelta: number): HotRankingSymbol[] {
  return records.map((item) => item.symbol === event.symbol ? quoteFromLiveEvent(item, event, volumeDelta) as HotRankingSymbol : item);
}

type LiveCandleVolume = {
  timestamp: string;
  volume: number;
};

type PendingQuoteEvent = {
  event: CandleEvent;
  volumeDelta: number;
};

const QUOTE_UI_FLUSH_INTERVAL_MS = 1000;
const QUOTE_DEV_LOG_INTERVAL_MS = 10000;

function quoteFromLiveEvent<T extends WatchlistSymbol>(item: T, event: CandleEvent, volumeDelta: number): T {
  const close = event.data.close;
  const previousPrice = item.lastPrice;
  const previousChange = item.changePercent;
  let changePercent = previousChange;
  if (typeof previousPrice === "number" && typeof previousChange === "number" && previousPrice !== 0) {
    const baseline = previousPrice / (1 + previousChange / 100);
    if (baseline) {
      changePercent = ((close - baseline) / baseline) * 100;
    }
  } else if (event.data.open) {
    changePercent = ((close - event.data.open) / event.data.open) * 100;
  }

  const currentVolume = typeof item.volume === "number" ? item.volume : 0;
  const sessionDollarVolume = "sessionDollarVolume" in item && typeof item.sessionDollarVolume === "number"
    ? item.sessionDollarVolume + volumeDelta * close
    : undefined;

  return {
    ...item,
    lastPrice: close,
    changePercent,
    volume: currentVolume + volumeDelta,
    ...(typeof sessionDollarVolume === "number" ? { sessionDollarVolume } : {})
  };
}

function liveVolumeDelta(event: CandleEvent, volumeMemory: Map<string, LiveCandleVolume>): number {
  const key = `${event.symbol}:${event.interval}`;
  const previous = volumeMemory.get(key);
  volumeMemory.set(key, { timestamp: event.data.timestamp, volume: event.data.volume });
  if (!previous || previous.timestamp !== event.data.timestamp) {
    return Math.max(0, event.data.volume);
  }
  return Math.max(0, event.data.volume - previous.volume);
}

function resolveQuoteSocketUrl(symbols: readonly string[], interval = "1m"): string {
  const params = new URLSearchParams({
    symbols: symbols.join(","),
    interval,
    maxHz: "1"
  });
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const isViteDevServer = window.location.hostname === "127.0.0.1" &&
    (window.location.port === "5173" || window.location.port === "5174");
  const host = isViteDevServer ? "127.0.0.1:8000" : window.location.host;
  return `${protocol}//${host}/ws/quotes?${params.toString()}`;
}

function runtimeReducer(state: LayoutRuntimeState, action: RuntimeAction): LayoutRuntimeState {
  if (action.kind === "agentLayoutProposal") {
    return applyLayoutProposal(state, action.proposal);
  }
  return executeCommand(state, action.command);
}

function chartRuntimeActionToDevLog(action: ChartRuntimeAction): ChartDevLogInput | null {
  switch (action.kind) {
    case "chart.snapshot.loaded": {
      const snapshot = action.snapshot;
      return {
        level: snapshot.dataStatus === "error" ? "warn" : "info",
        category: "chart-data",
        message: "Candle snapshot loaded",
        symbol: snapshot.symbol,
        interval: snapshot.interval,
        details: {
          candleCount: snapshot.candles.length,
          returnedCount: snapshot.returnedCount,
          storedCandleCount: snapshot.storedCandleCount,
          dataStatus: snapshot.dataStatus,
          backfillStatus: snapshot.backfillStatus,
          repairStatus: snapshot.repairStatus,
          source: snapshot.source,
          feed: snapshot.feed,
          sourceInterval: snapshot.sourceInterval,
          requestedLimit: snapshot.requestedLimit,
          requestedRange: snapshot.requestedRange,
          availableFrom: snapshot.availableFrom,
          availableTo: snapshot.availableTo,
          noDataBefore: snapshot.noDataBefore,
          oldestTimestamp: snapshot.oldestTimestamp,
          newestTimestamp: snapshot.newestTimestamp,
          hasMoreBefore: snapshot.hasMoreBefore,
          coverageSummary: summarizeChartCoverage(snapshot.coverage)
        }
      };
    }
    case "chart.snapshot.failed":
      return {
        level: "error",
        category: "chart-data",
        message: action.message,
        symbol: action.symbol,
        interval: action.interval
      };
    case "chart.data.status":
      return {
        level: chartDataLogLevel(action.status.state, action.status.backfillStatus),
        category: "backfill",
        message: action.status.message ?? `Chart data status: ${action.status.state}`,
        symbol: action.symbol,
        interval: action.interval,
        details: {
          state: action.status.state,
          backfillStatus: action.status.backfillStatus,
          canBackfill: action.status.canBackfill,
          source: action.status.source,
          feed: action.status.feed,
          sourceInterval: action.status.sourceInterval,
          requestedRange: action.status.requestedRange,
          hasMoreBefore: action.status.hasMoreBefore,
          availableFrom: action.status.availableFrom,
          availableTo: action.status.availableTo,
          noDataBefore: action.status.noDataBefore,
          repairStatus: action.status.repairStatus,
          coverageSummary: summarizeChartCoverage(action.status.coverage),
          coverage: action.status.coverage
        }
      };
    case "chart.stream.status":
      return {
        level: chartStreamLogLevel(action.status),
        category: "stream",
        message: `Stream status: ${action.status}`,
        symbol: action.symbol,
        interval: action.interval,
        details: action.message ? { message: action.message } : undefined
      };
    case "chart.command":
      if (action.command.type !== "chart.symbol.set" && action.command.type !== "chart.timeframe.set") {
        return null;
      }
      return {
        level: "debug",
        category: "runtime",
        message: `Chart command: ${action.command.type}`,
        chartDocumentId: action.command.target.chartDocumentId,
        panelId: action.command.target.panelId,
        details: {
          actor: action.command.actor,
          historyScope: action.command.historyScope,
          payload: action.command.payload
        }
      };
    case "chart.command.group":
      return {
        level: "debug",
        category: "runtime",
        message: action.label,
        details: {
          commandCount: action.commands.length,
          proposalId: action.proposalId
        }
      };
    case "chart.error":
      return {
        level: "error",
        category: "runtime",
        message: action.message,
        chartDocumentId: action.chartDocumentId
      };
    default:
      return null;
  }
}

function chartDataLogLevel(state: string, backfillStatus?: string): ChartDevLogLevel {
  if (state === "error" || backfillStatus === "failed" || backfillStatus === "unavailable") {
    return "error";
  }
  if (state === "empty" || backfillStatus === "queued" || backfillStatus === "running") {
    return "warn";
  }
  return "info";
}

function chartStreamLogLevel(status: string): ChartDevLogLevel {
  if (status === "error") {
    return "error";
  }
  if (status === "stale") {
    return "warn";
  }
  return status === "live" ? "info" : "debug";
}

function chartActionDedupeKey(action: ChartRuntimeAction): string | null {
  if (action.kind === "chart.data.status" || action.kind === "chart.stream.status") {
    return `${action.kind}:${action.symbol}:${action.interval}`;
  }
  return null;
}

export default function App() {
  const [state, dispatch] = useReducer(runtimeReducer, undefined, createInitialRuntimeState);
  const [chartRuntime, chartDispatch] = useReducer(chartRuntimeReducer, undefined, createInitialChartRuntimeState);
  const [activeSystemMode, setActiveSystemMode] = useState<SystemMode | null>(null);
  const [selectedAgentIds, setSelectedAgentIds] = useState<string[]>([]);
  const [settingsTab, setSettingsTab] = useState<SystemMenuTab>("layouts");
  const [agents, setAgents] = useState<AgentOption[]>(initialAgentOptions);
  const [editingAgentId, setEditingAgentId] = useState<string | undefined>();
  const [activeSymbol, setActiveSymbol] = useState<SupportedSymbol>(DEFAULT_CHART_SYMBOL);
  const [symbolSearchError, setSymbolSearchError] = useState<string | undefined>();
  const [watchlistSymbols, setWatchlistSymbols] = useState<WatchlistSymbol[]>(initialWatchlistSymbols);
  const [hotRankingSymbols, setHotRankingSymbols] = useState<HotRankingSymbol[]>([]);
  const [chartDevLogs, setChartDevLogs] = useState<ChartDevLogEntry[]>([]);
  const [symbolSearchQuery, setSymbolSearchQuery] = useState("");
  const [symbolSearchRefreshKey, setSymbolSearchRefreshKey] = useState(0);
  const [symbolOptions, setSymbolOptions] = useState<WatchlistSymbol[]>([]);
  const [knownSymbols, setKnownSymbols] = useState<WatchlistSymbol[]>([]);
  const [agentChartReference, setAgentChartReference] = useState<AgentChartReference | undefined>();
  const watchlistSeedAppliedRef = useRef(false);
  const initialWatchlistSyncedRef = useRef(false);
  const watchlistSymbolsRef = useRef<WatchlistSymbol[]>(watchlistSymbols);
  const liveCandleVolumeRef = useRef<Map<string, LiveCandleVolume>>(new Map());
  const pendingQuoteEventsRef = useRef<Map<string, PendingQuoteEvent>>(new Map());
  const userSelectedSymbolRef = useRef(false);
  const chartDevLogDedupeRef = useRef<Map<string, string>>(new Map());

  const appendChartDevLog = useCallback((entry: ChartDevLogInput) => {
    setChartDevLogs((current) => [
      createChartDevLogEntry(entry),
      ...current
    ].slice(0, CHART_DEV_LOG_LIMIT));
  }, []);

  const appendChartActionDevLog = useCallback((action: ChartRuntimeAction) => {
    const entry = chartRuntimeActionToDevLog(action);
    if (!entry) {
      return;
    }

    const dedupeKey = chartActionDedupeKey(action);
    if (dedupeKey) {
      const signature = `${entry.level}|${entry.message}|${JSON.stringify(entry.details ?? {})}`;
      if (chartDevLogDedupeRef.current.get(dedupeKey) === signature) {
        return;
      }
      chartDevLogDedupeRef.current.set(dedupeKey, signature);
    }

    appendChartDevLog(entry);
  }, [appendChartDevLog]);

  const selectedPanel = useMemo(
    () => state.layout.panels.find((panel) => panel.id === state.layout.selectedPanelId),
    [state.layout.panels, state.layout.selectedPanelId]
  );

  const runCommand = useCallback((command: LayoutCommand) => dispatch({ kind: "command", command }), []);
  const runLayoutProposal = useCallback((proposal: LayoutProposal) => dispatch({ kind: "agentLayoutProposal", proposal }), []);
  const runChartAction = useCallback((action: ChartRuntimeAction) => {
    appendChartActionDevLog(action);
    chartDispatch(action);
  }, [appendChartActionDevLog]);

  useEffect(() => {
    runChartAction({ kind: "chart.ensureDocuments", panels: state.layout.panels });
  }, [runChartAction, state.layout.panels]);

  useEffect(() => {
    if (agentChartReference && !isAgentChartReferenceAvailable(state.layout.panels, agentChartReference)) {
      setAgentChartReference(undefined);
    }
  }, [agentChartReference, state.layout.panels]);

  useEffect(() => {
    watchlistSymbolsRef.current = watchlistSymbols;
    setKnownSymbols((current) => mergeSymbolRecords(current, watchlistSymbols));
  }, [watchlistSymbols]);

  useEffect(() => {
    if (initialWatchlistSyncedRef.current) {
      return;
    }

    initialWatchlistSyncedRef.current = true;
    putWatchlistSymbols(watchlistSymbols)
      .then((payload) => {
        if (!payload) {
          return;
        }
        const summaries = normalizeWatchlistPayload(payload);
        setWatchlistSymbols((current) => refreshWatchlistRecords(current, summaries));
        setKnownSymbols((current) => mergeSymbolRecords(current, summaries));
      })
      .catch(() => undefined);
  }, [watchlistSymbols]);

  useEffect(() => {
    let cancelled = false;

    const loadWatchlist = () => {
      fetch(watchlistRequestUrl(watchlistSymbolsRef.current))
        .then((response) => {
          if (!response.ok) {
            throw new Error(`Watch List API returned ${response.status}`);
          }
          return response.json() as Promise<unknown>;
        })
        .then((payload) => {
          if (!cancelled) {
            const symbols = normalizeWatchlistPayload(payload);
            setWatchlistSymbols((current) => {
              return refreshWatchlistRecords(current, symbols);
            });
            setSymbolOptions((current) => current.length > 0 ? current : symbols);
            setKnownSymbols((current) => mergeSymbolRecords(current, symbols));
          }
        })
        .catch(() => {
          if (!cancelled) {
            setWatchlistSymbols((current) => current);
            setSymbolOptions((current) => current);
          }
        });
    };

    loadWatchlist();
    const timer = window.setInterval(loadWatchlist, 15000);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const loadHotRanking = () => {
      fetch("/api/charts/hot-symbols?limit=10")
        .then((response) => {
          if (!response.ok) {
            throw new Error(`Hot ranking API returned ${response.status}`);
          }
          return response.json() as Promise<unknown>;
        })
        .then((payload) => {
          if (!cancelled) {
            const symbols = normalizeHotRankingPayload(payload);
            setHotRankingSymbols(symbols);
            setKnownSymbols((current) => mergeSymbolRecords(current, symbols));
            appendChartDevLog({
              level: symbols.length >= 10 ? "info" : "warn",
              category: "stream",
              message: "Hot ranking loaded",
              details: {
                requestedLimit: 10,
                symbolCount: symbols.length,
                symbols: symbols.map((item) => item.symbol)
              }
            });
          }
        })
        .catch((error: unknown) => {
          if (!cancelled) {
            setHotRankingSymbols((current) => current);
            appendChartDevLog({
              level: "error",
              category: "stream",
              message: "Hot ranking request failed",
              details: {
                error: error instanceof Error ? error.message : String(error)
              }
            });
          }
        });
    };

    loadHotRanking();
    const timer = window.setInterval(loadHotRanking, 60000);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [appendChartDevLog]);

  const quoteStreamSymbolsKey = useMemo(() => {
    const symbols = new Set<string>();
    watchlistSymbols.slice(0, QUOTE_WATCHLIST_SYMBOL_LIMIT).forEach((item) => symbols.add(item.symbol));
    hotRankingSymbols.slice(0, QUOTE_HOT_SYMBOL_LIMIT).forEach((item) => symbols.add(item.symbol));
    return Array.from(symbols).join("|");
  }, [hotRankingSymbols, watchlistSymbols]);

  useEffect(() => {
    if (typeof window === "undefined" || !("WebSocket" in window) || !quoteStreamSymbolsKey) {
      return undefined;
    }

    const reconnectTimers: number[] = [];
    let closed = false;
    let receivedCount = 0;
    let coalescedCount = 0;
    let devLogReceivedCount = 0;
    let devLogCoalescedCount = 0;
    let devLogFlushCount = 0;
    let devLogFlushedSymbols = 0;
    let invalidQuoteEventLogged = false;
    const symbols = quoteStreamSymbolsKey.split("|").filter(Boolean);
    const flushQuoteEvents = () => {
      const pendingEvents = Array.from(pendingQuoteEventsRef.current.values());
      pendingQuoteEventsRef.current.clear();
      if (!pendingEvents.length) {
        return;
      }
      setWatchlistSymbols((current) => pendingEvents.reduce(
        (next, item) => applyRealtimeQuote(next, item.event, item.volumeDelta),
        current
      ));
      setHotRankingSymbols((current) => pendingEvents.reduce(
        (next, item) => applyRealtimeHotQuote(next, item.event, item.volumeDelta),
        current
      ));
      devLogReceivedCount += receivedCount;
      devLogCoalescedCount += coalescedCount;
      devLogFlushCount += 1;
      devLogFlushedSymbols += pendingEvents.length;
      receivedCount = 0;
      coalescedCount = 0;
    };
    const flushQuoteDevLog = () => {
      if (devLogCoalescedCount <= 0) {
        devLogReceivedCount = 0;
        devLogFlushCount = 0;
        devLogFlushedSymbols = 0;
        return;
      }
      appendChartDevLog({
        level: "debug",
        category: "stream",
        message: "Quote UI events coalesced",
        details: {
          symbolCount: symbols.length,
          flushCount: devLogFlushCount,
          flushedSymbols: devLogFlushedSymbols,
          receivedCount: devLogReceivedCount,
          coalescedCount: devLogCoalescedCount,
          intervalMs: QUOTE_DEV_LOG_INTERVAL_MS
        }
      });
      devLogReceivedCount = 0;
      devLogCoalescedCount = 0;
      devLogFlushCount = 0;
      devLogFlushedSymbols = 0;
    };
    const flushTimer = window.setInterval(flushQuoteEvents, QUOTE_UI_FLUSH_INTERVAL_MS);
    const devLogTimer = window.setInterval(flushQuoteDevLog, QUOTE_DEV_LOG_INTERVAL_MS);
    let socket: WebSocket | undefined;
    const connect = () => {
      if (closed) {
        return;
      }
      appendChartDevLog({
        level: "debug",
        category: "stream",
        message: "Opening quote websocket",
        details: {
          symbolCount: symbols.length,
          interval: "1m",
          maxHz: 1
        }
      });
      socket = new WebSocket(resolveQuoteSocketUrl(symbols, "1m"));
      socket.onopen = () => {
        appendChartDevLog({
          level: "info",
          category: "stream",
          message: "Quote websocket opened",
          details: {
            symbolCount: symbols.length,
            interval: "1m",
            maxHz: 1
          }
        });
      };
      socket.onmessage = (message) => {
        try {
          const payload = JSON.parse(message.data);
          if (isRealtimeControlPayload(payload)) {
            return;
          }
          const event = normalizeCandleEvent(payload);
          if (event.interval !== "1m") {
            return;
          }
          const volumeMemory = liveCandleVolumeRef.current;
          const volumeDelta = liveVolumeDelta(event, volumeMemory);
          const previous = pendingQuoteEventsRef.current.get(event.symbol);
          if (previous) {
            coalescedCount += 1;
          }
          receivedCount += 1;
          pendingQuoteEventsRef.current.set(event.symbol, {
            event,
            volumeDelta: (previous?.volumeDelta ?? 0) + volumeDelta
          });
        } catch (error) {
          if (!invalidQuoteEventLogged) {
            invalidQuoteEventLogged = true;
            appendChartDevLog({
              level: "warn",
              category: "stream",
              message: "Invalid auxiliary quote event ignored",
              details: {
                error: error instanceof Error ? error.message : String(error)
              }
            });
          }
        }
      };
      socket.onerror = () => {
        appendChartDevLog({
          level: "warn",
          category: "stream",
          message: "Quote websocket transport error; reconnecting",
          details: {
            symbolCount: symbols.length,
            interval: "1m",
            maxHz: 1
          }
        });
      };
      socket.onclose = () => {
        if (!closed) {
          appendChartDevLog({
            level: "warn",
            category: "stream",
            message: "Quote websocket closed; reconnect scheduled",
            details: {
              symbolCount: symbols.length,
              reconnectDelayMs: 3000
            }
          });
          reconnectTimers.push(window.setTimeout(connect, 3000));
        }
      };
    };
    connect();

    return () => {
      closed = true;
      flushQuoteEvents();
      flushQuoteDevLog();
      window.clearInterval(flushTimer);
      window.clearInterval(devLogTimer);
      reconnectTimers.forEach((timer) => window.clearTimeout(timer));
      socket?.close();
    };
  }, [appendChartDevLog, quoteStreamSymbolsKey]);

  useEffect(() => {
    const query = symbolSearchQuery.trim();

    let cancelled = false;
    const controller = new AbortController();
    const params = new URLSearchParams({ q: query, limit: query ? "20" : "100" });

    fetch(`/api/market/symbols/search?${params.toString()}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`종목 검색 API 응답 오류 ${response.status}`);
        }
        return response.json() as Promise<unknown>;
      })
      .then((payload) => {
        if (!cancelled) {
          const symbols = normalizeWatchlistPayload(payload);
          setSymbolOptions(symbols);
          setKnownSymbols((current) => mergeSymbolRecords(current, symbols));
        }
      })
      .catch(() => {
        if (!cancelled) {
          const normalizedQuery = query.toUpperCase();
          setSymbolOptions(watchlistSymbolsRef.current.filter((item) =>
            item.symbol.includes(normalizedQuery) || item.name.toUpperCase().includes(normalizedQuery)
          ));
        }
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [symbolSearchQuery, symbolSearchRefreshKey]);

  const activeChartPanel = useMemo(
    () => findTargetChartPanel(state.layout.panels, state.layout.selectedPanelId),
    [state.layout.panels, state.layout.selectedPanelId]
  );

  const activeChartDocument = useMemo(
    () => activeChartPanel ? getChartDocumentForPanel(chartRuntime, activeChartPanel) : null,
    [activeChartPanel, chartRuntime]
  );

  const symbolUniverseKey = useMemo(
    () => Array.from(new Set(knownSymbols.map((item) => item.symbol))).join("|"),
    [knownSymbols]
  );
  const symbolUniverse = useMemo(
    () => symbolUniverseKey.split("|").filter(Boolean) as SupportedSymbol[],
    [symbolUniverseKey]
  );

  const orderChartSymbols = useMemo(() => {
    const bySymbol = new Map<SupportedSymbol, WatchlistSymbol>();
    for (const panel of state.layout.panels) {
      if (panel.type !== "chart") {
        continue;
      }
      const chartDocument = getChartDocumentForPanel(chartRuntime, panel);
      const symbol = normalizeSupportedSymbol(chartDocument.symbol);
      if (!symbol || bySymbol.has(symbol)) {
        continue;
      }
      const candles = getCandlesForDocument(chartRuntime, chartDocument);
      const latestCandle = candles[candles.length - 1];
      const latestClose = latestCandle && Number.isFinite(latestCandle.close) ? latestCandle.close : undefined;
      const known = knownSymbols.find((item) => item.symbol === symbol);
      const fallback = getSymbolMeta(symbol);
      bySymbol.set(symbol, {
        symbol,
        name: known?.name ?? fallback.name,
        market: known?.market ?? fallback.market,
        lastPrice: typeof known?.lastPrice === "number" ? known.lastPrice : latestClose,
        changePercent: known?.changePercent,
        volume: known?.volume
      });
    }
    return Array.from(bySymbol.values());
  }, [chartRuntime, knownSymbols, state.layout.panels]);

  const syncWatchlistSymbols = useCallback((symbols: WatchlistSymbol[]) => {
    writeStoredWatchlistSymbols(symbols);
    putWatchlistSymbols(symbols)
      .then((payload) => {
        if (!payload) {
          return;
        }
        const summaries = normalizeWatchlistPayload(payload);
        setWatchlistSymbols((current) => refreshWatchlistRecords(current, summaries));
        setKnownSymbols((current) => mergeSymbolRecords(current, summaries));
      })
      .catch(() => undefined);
  }, []);

  const toggleWatchlistSymbol = useCallback((symbolValue: string) => {
    const symbol = normalizeSupportedSymbol(symbolValue);
    if (!symbol) {
      return;
    }

    setWatchlistSymbols((current) => {
      let next: WatchlistSymbol[];
      if (current.some((item) => item.symbol === symbol)) {
        next = current.filter((item) => item.symbol !== symbol);
      } else {
        const known = knownSymbols.find((item) => item.symbol === symbol);
        const fallback = getSymbolMeta(symbol);
        next = [
          ...current,
          known ?? {
            symbol,
            name: fallback.name,
            market: fallback.market
          }
        ];
      }

      syncWatchlistSymbols(next);
      return next;
    });
  }, [knownSymbols, syncWatchlistSymbols]);

  const refreshSymbolOptions = useCallback((query: string) => {
    setSymbolSearchQuery(query);
    setSymbolSearchRefreshKey((current) => current + 1);
  }, []);

  useEffect(() => {
    const normalized = activeChartDocument ? normalizeSupportedSymbol(activeChartDocument.symbol) : null;
    if (normalized && normalized !== activeSymbol) {
      setActiveSymbol(normalized);
    }
  }, [activeChartDocument?.symbol, activeSymbol]);

  const selectSymbol = useCallback((value: string, options?: { source?: "system" | "user" }): boolean => {
    const symbol = normalizeSupportedSymbol(value);
    if (!symbol) {
      setSymbolSearchError("유효한 종목 코드를 입력하세요.");
      return false;
    }

    if (options?.source !== "system") {
      userSelectedSymbolRef.current = true;
    }
    setActiveSymbol(symbol);
    setSymbolSearchError(undefined);

    const chartPanel = findTargetChartPanel(state.layout.panels, state.layout.selectedPanelId);
    if (!chartPanel) {
      return true;
    }

    const chartDocument = getChartDocumentForPanel(chartRuntime, chartPanel);
    runChartAction({ kind: "chart.ensureDocuments", panels: state.layout.panels });
    runChartAction({
      kind: "chart.command",
      command: makeChartCommand("chart.symbol.set", "user", {
        panelId: chartPanel.id,
        chartDocumentId: chartDocument.id
      }, { symbol }, undefined, "external")
    });
    return true;
  }, [chartRuntime, runChartAction, state.layout.panels, state.layout.selectedPanelId]);

  useEffect(() => {
    if (watchlistSeedAppliedRef.current || userSelectedSymbolRef.current || watchlistSymbols.length === 0) {
      return;
    }

    watchlistSeedAppliedRef.current = true;
    if (!watchlistSymbols.some((item) => item.symbol === activeSymbol)) {
      selectSymbol(watchlistSymbols[0].symbol, { source: "system" });
    }
  }, [activeSymbol, selectSymbol, watchlistSymbols]);

  const closeSystemPanel = () => {
    setSelectedAgentIds([]);
    setAgentChartReference(undefined);
    setEditingAgentId(undefined);
    setActiveSystemMode(null);
  };

  const toggleWatchlist = () => {
    setSelectedAgentIds([]);
    setAgentChartReference(undefined);
    setEditingAgentId(undefined);
    setActiveSystemMode((current) => (current === "watchlist" ? null : "watchlist"));
  };

  const toggleSettings = () => {
    setSelectedAgentIds([]);
    setAgentChartReference(undefined);
    setEditingAgentId(undefined);
    setSettingsTab("layouts");
    setActiveSystemMode((current) => (current === "settings" ? null : "settings"));
  };

  const toggleNotifications = () => {
    setSelectedAgentIds([]);
    setAgentChartReference(undefined);
    setEditingAgentId(undefined);
    setActiveSystemMode((current) => (current === "notifications" ? null : "notifications"));
  };

  const togglePrimaryAgent = () => {
    const primaryAgentId = "agent-01";
    const primaryAgentActive = activeSystemMode === "agents" && selectedAgentIds.includes(primaryAgentId);

    setEditingAgentId(undefined);
    if (primaryAgentActive) {
      setSelectedAgentIds([]);
      setAgentChartReference(undefined);
      setActiveSystemMode(null);
      return;
    }

    setSelectedAgentIds([primaryAgentId]);
    setAgentChartReference(undefined);
    setActiveSystemMode("agents");
  };

  const updateAgent = (agentId: string, patch: AgentUpdatePatch) => {
    setAgents((current) => current.map((agent) => (agent.id === agentId ? { ...agent, ...patch } : agent)));
  };

  const askAgentFromChart = useCallback((panelId: string, chartDocumentId: string) => {
    setSelectedAgentIds(["agent-01"]);
    setAgentChartReference({ panelId, chartDocumentId, draftSeed: DEFAULT_AGENT_DRAFT_SEED });
    setEditingAgentId(undefined);
    setActiveSystemMode("agents");
  }, []);

  const addAgent = () => {
    setAgents((current) => {
      if (current.length >= 4) {
        return current;
      }

      const usedNumbers = new Set(
        current
          .map((agent) => Number(agent.id.replace("agent-", "")))
          .filter((value) => Number.isFinite(value))
      );
      const nextNumber = [1, 2, 3, 4].find((value) => !usedNumbers.has(value)) ?? current.length + 1;
      return [
        ...current,
        {
          id: `agent-${String(nextNumber).padStart(2, "0")}`,
          label: `AI ${String(nextNumber).padStart(2, "0")}`,
          description: "새 작업 보조 AI입니다.",
          iconUrl: `/assets/agent-icons/agent-${String(nextNumber).padStart(2, "0")}.svg`
        }
      ];
    });
  };

  const deleteAgent = (agentId: string) => {
    setAgents((current) => current.filter((agent) => agent.id !== agentId));
    setSelectedAgentIds((current) => {
      const next = current.filter((id) => id !== agentId);
      setActiveSystemMode((mode) => (mode === "agents" ? (next.length === 0 ? null : "agents") : mode));
      return next;
    });
    setEditingAgentId(undefined);
  };

  return (
    <main className="app-shell">
      <TopAppBar
        aiActive={activeSystemMode === "agents" && selectedAgentIds.includes("agent-01")}
        watchlistActive={activeSystemMode === "watchlist"}
        settingsActive={activeSystemMode === "settings"}
        notificationsActive={activeSystemMode === "notifications"}
        activeSymbol={activeSymbol}
        symbolOptions={symbolOptions}
        symbolSearchError={symbolSearchError}
        onToggleNotifications={toggleNotifications}
        onTogglePrimaryAgent={togglePrimaryAgent}
        onToggleWatchlist={toggleWatchlist}
        onToggleSettings={toggleSettings}
        onSymbolQueryChange={setSymbolSearchQuery}
        onSymbolOptionsRequest={refreshSymbolOptions}
        onSymbolSearch={selectSymbol}
        onCommand={runCommand}
      />

      <section className="workspace-area" aria-label="GOPS 작업 화면">
        <WorkspaceGrid
          layout={state.layout}
          selectedPanelId={selectedPanel?.id}
          systemMode={activeSystemMode}
          settingsTab={settingsTab}
          agents={agents}
          selectedAgentIds={selectedAgentIds}
          referencedChartTarget={agentChartReference}
          editingAgentId={editingAgentId}
          savedLayouts={state.savedLayouts}
          activeSymbol={activeSymbol}
          watchlistSymbols={watchlistSymbols}
          hotRankingSymbols={hotRankingSymbols}
          knownSymbols={knownSymbols}
          orderChartSymbols={orderChartSymbols}
          symbolOptions={symbolOptions}
          symbolUniverse={symbolUniverse}
          backfillEligibleSymbols={symbolUniverse}
          chartRuntime={chartRuntime}
          chartDevLogs={chartDevLogs}
          chartAutoApplyEnabled={state.layout.settings.llmLayoutAutoApply}
          onSettingsTabChange={setSettingsTab}
          onEditAgent={setEditingAgentId}
          onUpdateAgent={updateAgent}
          onAddAgent={addAgent}
          onDeleteAgent={deleteAgent}
          onCloseSystemPanel={closeSystemPanel}
          onSelectSymbol={selectSymbol}
          onSymbolOptionsRequest={refreshSymbolOptions}
          onCommand={runCommand}
          onLayoutProposal={runLayoutProposal}
          onChartAction={runChartAction}
          onChartDevLog={appendChartDevLog}
          onAskAgentFromChart={askAgentFromChart}
          onToggleWatchlistSymbol={toggleWatchlistSymbol}
        />
      </section>

      <MarketTicker />
    </main>
  );
}

export function command(type: Parameters<typeof makeCommand>[0], payload: Record<string, unknown> = {}) {
  return makeCommand(type, "user", payload);
}
