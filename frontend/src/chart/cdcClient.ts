import type { CandleEventDto, CandleQueryResponseDto, ChartInterval, ChartSymbolsResponseDto } from "./types";

export type CandleQuery = {
  symbol: string;
  interval: ChartInterval;
  limit: number;
  before?: string;
  from?: string;
  to?: string;
  ma?: number[];
};

export async function fetchCandles(query: CandleQuery, signal?: AbortSignal): Promise<CandleQueryResponseDto> {
  const params = new URLSearchParams({
    symbol: query.symbol,
    interval: query.interval,
    limit: String(query.limit),
    session: "regular",
    ma: (query.ma ?? [5, 20, 60]).join(",")
  });
  if (query.before) {
    params.set("before", query.before);
  }
  if (query.from && query.to) {
    params.set("from", query.from);
    params.set("to", query.to);
  }
  const response = await fetch(`/api/charts/candles?${params.toString()}`, { signal });
  if (!response.ok) {
    throw new Error(`Candle API failed: ${response.status}`);
  }
  return normalizeCandleResponse(await response.json());
}

export async function fetchSymbols(signal?: AbortSignal): Promise<ChartSymbolsResponseDto> {
  const response = await fetch("/api/charts/symbols", { signal });
  if (!response.ok) {
    throw new Error(`Symbol API failed: ${response.status}`);
  }
  return normalizeSymbolsResponse(await response.json());
}

export function openChartSocket(
  symbol: string,
  interval: ChartInterval,
  onEvent: (event: CandleEventDto) => void,
  onState: (state: "connecting" | "live" | "idle" | "error") => void
): () => void {
  let closed = false;
  let socket: WebSocket | null = null;
  let reconnectTimer: number | undefined;
  let reconnectAttempts = 0;

  const connect = () => {
    if (closed) {
      return;
    }
    const nextSocket = new WebSocket(chartSocketUrl(symbol, interval));
    socket = nextSocket;
    onState("connecting");
    nextSocket.onopen = () => {
      reconnectAttempts = 0;
      onState("idle");
    };
    nextSocket.onmessage = (message) => {
      try {
        const payload = JSON.parse(message.data);
        if (payload.type === "HEARTBEAT") {
          onState("idle");
          return;
        }
        onEvent(normalizeCandleEvent(payload));
        onState("live");
      } catch {
        onState("error");
      }
    };
    nextSocket.onerror = () => {
      onState("error");
      nextSocket.close();
    };
    nextSocket.onclose = () => {
      if (closed || socket !== nextSocket) {
        return;
      }
      onState("error");
      reconnectAttempts += 1;
      reconnectTimer = window.setTimeout(connect, reconnectDelayMs(reconnectAttempts));
    };
  };

  connect();

  return () => {
    closed = true;
    if (reconnectTimer !== undefined) {
      window.clearTimeout(reconnectTimer);
    }
    socket?.close();
  };
}

function chartSocketUrl(symbol: string, interval: ChartInterval): string {
  const params = new URLSearchParams({ symbol, interval });
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws/charts?${params.toString()}`;
}

function reconnectDelayMs(attempts: number): number {
  return Math.min(3_000, 250 * 2 ** Math.min(4, Math.max(0, attempts - 1)));
}

function normalizeCandleResponse(payload: unknown): CandleQueryResponseDto {
  if (!payload || typeof payload !== "object") {
    throw new Error("Invalid candle response");
  }
  const source = payload as CandleQueryResponseDto;
  if (!source.symbol || !source.interval || !Array.isArray(source.candles)) {
    throw new Error("Candle response missing required fields");
  }
  return {
    ...source,
    candles: source.candles.filter((item) =>
      item &&
      typeof item.timestamp === "string" &&
      Number.isFinite(item.open) &&
      Number.isFinite(item.high) &&
      Number.isFinite(item.low) &&
      Number.isFinite(item.close) &&
      Number.isFinite(item.volume)
    ).sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp))
  };
}

function normalizeSymbolsResponse(payload: unknown): ChartSymbolsResponseDto {
  if (!payload || typeof payload !== "object") {
    throw new Error("Invalid symbols response");
  }
  const source = payload as ChartSymbolsResponseDto;
  if (!Array.isArray(source.symbols)) {
    throw new Error("Symbols response missing required fields");
  }
  return {
    symbols: source.symbols.filter((item) =>
      item &&
      typeof item.symbol === "string" &&
      typeof item.name === "string"
    )
  };
}

function normalizeCandleEvent(payload: unknown): CandleEventDto {
  if (!payload || typeof payload !== "object") {
    throw new Error("Invalid candle event");
  }
  const source = payload as CandleEventDto;
  if (!source.type || !source.symbol || !source.interval || !source.data) {
    throw new Error("Candle event missing required fields");
  }
  return source;
}
