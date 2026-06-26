export const SUPPORTED_SYMBOLS = ["AAPL", "MSFT", "NVDA", "TSLA", "SPY"] as const;

export type SupportedSymbol = typeof SUPPORTED_SYMBOLS[number];

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

const supportedSymbolSet = new Set<string>(SUPPORTED_SYMBOLS);

const defaultSymbols: SymbolMeta[] = [
  { symbol: "AAPL", name: "Apple Inc.", market: "NASDAQ" },
  { symbol: "MSFT", name: "Microsoft Corp.", market: "NASDAQ" },
  { symbol: "NVDA", name: "NVIDIA Corp.", market: "NASDAQ" },
  { symbol: "TSLA", name: "Tesla Inc.", market: "NASDAQ" },
  { symbol: "SPY", name: "SPDR S&P 500 ETF", market: "NYSEARCA" }
];

export function normalizeSupportedSymbol(value: string): SupportedSymbol | null {
  const symbol = value.trim().toUpperCase();
  return supportedSymbolSet.has(symbol) ? (symbol as SupportedSymbol) : null;
}

export function defaultWatchlistSymbols(): WatchlistSymbol[] {
  return defaultSymbols.map((item) => ({ ...item }));
}

export function getSymbolMeta(value: string): SymbolMeta {
  const symbol = normalizeSupportedSymbol(value);
  const fallbackSymbol = symbol ?? "AAPL";
  const fallback = defaultSymbols.find((item) => item.symbol === fallbackSymbol) ?? defaultSymbols[0];
  return fallback ? { ...fallback } : { symbol: "AAPL", name: "AAPL", market: "UNKNOWN" };
}

export function getSymbolName(value: string): string {
  const symbol = normalizeSupportedSymbol(value);
  return symbol ? getSymbolMeta(symbol).name : value.toUpperCase();
}

export function normalizeWatchlistPayload(payload: unknown): WatchlistSymbol[] {
  if (!payload || typeof payload !== "object") {
    return defaultWatchlistSymbols();
  }

  const source = payload as Record<string, unknown>;
  const records = Array.isArray(source.symbols) ? source.symbols : [];
  const normalized = records
    .map(normalizeWatchlistRecord)
    .filter((item): item is WatchlistSymbol => Boolean(item));
  const bySymbol = new Map(normalized.map((item) => [item.symbol, item]));

  return defaultSymbols.map((fallback) => bySymbol.get(fallback.symbol) ?? { ...fallback });
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

function readOptionalNumber(value: unknown, key: "lastPrice" | "changePercent" | "volume") {
  return typeof value === "number" && Number.isFinite(value) ? { [key]: value } : {};
}
