export type SupportedSymbol = string;

export const DEFAULT_CHART_SYMBOL: SupportedSymbol = "AAPL";

export type SymbolMeta = {
  symbol: SupportedSymbol;
  name: string;
  market: string;
};

export type WatchlistSymbol = SymbolMeta & {
  lastPrice?: number;
  changePercent?: number;
  volume?: number;
};

export type HotRankingSymbol = WatchlistSymbol & {
  rank: number;
  sessionDollarVolume?: number;
  rankReason?: string;
};

const DEFAULT_SYMBOL_METADATA: Record<string, SymbolMeta> = {
  AAPL: { symbol: "AAPL", name: "Apple Inc.", market: "NASDAQ" },
  MSFT: { symbol: "MSFT", name: "Microsoft Corporation", market: "NASDAQ" },
  XOM: { symbol: "XOM", name: "Exxon Mobil Corporation", market: "NYSE" },
  AMZN: { symbol: "AMZN", name: "Amazon.com, Inc.", market: "NASDAQ" },
  GOOGL: { symbol: "GOOGL", name: "Alphabet Inc. Class A", market: "NASDAQ" },
  META: { symbol: "META", name: "Meta Platforms, Inc.", market: "NASDAQ" },
  TSLA: { symbol: "TSLA", name: "Tesla, Inc.", market: "NASDAQ" },
  JPM: { symbol: "JPM", name: "JPMorgan Chase & Co.", market: "NYSE" },
  UNH: { symbol: "UNH", name: "UnitedHealth Group Incorporated", market: "NYSE" },
  "BRK.B": { symbol: "BRK.B", name: "Berkshire Hathaway Inc. Class B", market: "NYSE" }
};

export const DEFAULT_WATCHLIST_SYMBOLS: WatchlistSymbol[] = [
  "AAPL",
  "MSFT",
  "XOM",
  "AMZN",
  "GOOGL",
  "META",
  "TSLA",
  "JPM",
  "UNH",
  "BRK.B"
].map((symbol) => ({ ...DEFAULT_SYMBOL_METADATA[symbol] }));

const symbolPattern = /^[A-Z][A-Z0-9]{0,9}(\.[A-Z])?$/;

export function normalizeSupportedSymbol(value: string): SupportedSymbol | null {
  const symbol = value.trim().toUpperCase();
  return symbolPattern.test(symbol) ? symbol : null;
}

export function emptyWatchlistSymbols(): WatchlistSymbol[] {
  return [];
}

export function defaultWatchlistSymbols(): WatchlistSymbol[] {
  return DEFAULT_WATCHLIST_SYMBOLS.map((item) => ({ ...item }));
}

export function getSymbolMeta(value: string): SymbolMeta {
  const symbol = normalizeSupportedSymbol(value);
  if (!symbol) {
    return { symbol: DEFAULT_CHART_SYMBOL, name: DEFAULT_CHART_SYMBOL, market: "US" };
  }

  const knownDefault = DEFAULT_SYMBOL_METADATA[symbol];
  return knownDefault ? { ...knownDefault } : { symbol, name: symbol, market: "US" };
}

export function getSymbolName(value: string): string {
  const symbol = normalizeSupportedSymbol(value);
  return symbol ? getSymbolMeta(symbol).name : value.toUpperCase();
}

export function normalizeWatchlistPayload(payload: unknown): WatchlistSymbol[] {
  if (!payload || typeof payload !== "object") {
    return emptyWatchlistSymbols();
  }

  const source = payload as Record<string, unknown>;
  const records = Array.isArray(source.symbols) ? source.symbols : [];
  const normalized = records
    .map(normalizeWatchlistRecord)
    .filter((item): item is WatchlistSymbol => Boolean(item));

  if (normalized.length) {
    return normalized;
  }

  return emptyWatchlistSymbols();
}

export function normalizeHotRankingPayload(payload: unknown): HotRankingSymbol[] {
  if (!payload || typeof payload !== "object") {
    return [];
  }

  const source = payload as Record<string, unknown>;
  const records = Array.isArray(source.symbols) ? source.symbols : [];
  return records
    .map(normalizeHotRankingRecord)
    .filter((item): item is HotRankingSymbol => Boolean(item));
}

function normalizeWatchlistRecord(record: unknown): WatchlistSymbol | null {
  if (!record || typeof record !== "object") {
    return null;
  }

  const source = record as Record<string, unknown>;
  const symbol = normalizeSupportedSymbol(typeof source.symbol === "string" ? source.symbol : "");
  if (!symbol) {
    return null;
  }

  const fallback = getSymbolMeta(symbol);
  return {
    symbol,
    name: typeof source.name === "string" && source.name.trim() ? source.name : fallback.name,
    market: typeof source.market === "string" && source.market.trim() ? source.market.trim().toUpperCase() : fallback.market,
    ...readOptionalNumber(source.lastPrice, "lastPrice"),
    ...readOptionalNumber(source.changePercent, "changePercent"),
    ...readOptionalNumber(source.volume, "volume")
  };
}

function normalizeHotRankingRecord(record: unknown): HotRankingSymbol | null {
  const watchlistRecord = normalizeWatchlistRecord(record);
  if (!watchlistRecord || !record || typeof record !== "object") {
    return null;
  }

  const source = record as Record<string, unknown>;
  const rank = typeof source.rank === "number" && Number.isFinite(source.rank) ? Math.max(1, Math.floor(source.rank)) : null;
  if (rank === null) {
    return null;
  }

  return {
    ...watchlistRecord,
    rank,
    ...readOptionalNumber(source.sessionDollarVolume, "sessionDollarVolume"),
    rankReason: typeof source.rankReason === "string" ? source.rankReason : undefined
  };
}

function readOptionalNumber(value: unknown, key: "lastPrice" | "changePercent" | "volume" | "sessionDollarVolume") {
  return typeof value === "number" && Number.isFinite(value) ? { [key]: value } : {};
}
