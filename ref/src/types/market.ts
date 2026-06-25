export type SymbolCode = string;
export type Timeframe = "1s" | "5s" | "15s" | "1m" | "5m" | "15m" | "1h" | "1d";
export type Provider = "dummy" | "alpaca";
export type MarketSession = "pre" | "regular" | "post" | "closed";
export type IsoTimestamp = string;

export type NormalizedMarketEvent =
  | TradeEvent
  | QuoteEvent
  | BarEvent
  | UpdatedBarEvent
  | StatusEvent;

export interface TradeEvent {
  type: "trade";
  provider: Provider;
  symbol: SymbolCode;
  tradeId: string;
  price: number;
  size: number;
  exchange?: string;
  conditions: string[];
  tape?: string;
  timestamp: IsoTimestamp;
  receivedAt: IsoTimestamp;
}

export interface QuoteEvent {
  type: "quote";
  provider: Provider;
  symbol: SymbolCode;
  bidPrice: number;
  bidSize: number;
  bidExchange?: string;
  askPrice: number;
  askSize: number;
  askExchange?: string;
  conditions: string[];
  tape?: string;
  timestamp: IsoTimestamp;
  receivedAt: IsoTimestamp;
}

export interface BarEvent {
  type: "bar";
  provider: Provider;
  symbol: SymbolCode;
  timeframe: Timeframe;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  vwap?: number;
  tradeCount?: number;
  timestamp: IsoTimestamp;
  receivedAt: IsoTimestamp;
}

export interface UpdatedBarEvent extends Omit<BarEvent, "type"> {
  type: "updatedBar";
}

export interface StatusEvent {
  type: "status";
  provider: Provider;
  symbol: SymbolCode;
  statusCode: string;
  statusMessage: string;
  reasonCode?: string;
  reasonMessage?: string;
  timestamp: IsoTimestamp;
  receivedAt: IsoTimestamp;
}

export interface Candle {
  symbol: SymbolCode;
  timeframe: Timeframe;
  timestamp: IsoTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  vwap?: number;
  tradeCount?: number;
  finalized: boolean;
}

export interface MarketSnapshotResponse {
  provider: Provider;
  timeframe: Timeframe;
  generatedAt: IsoTimestamp;
  symbols: SymbolCode[];
  candlesBySymbol: Record<SymbolCode, Candle[]>;
}

export interface MarketSubscribeMessage {
  type: "subscribe";
  requestId: string;
  symbols: SymbolCode[];
  timeframe: Timeframe;
  includeTrades: boolean;
  includeQuotes: boolean;
  includeBars: boolean;
  snapshotLimit: number;
}

export interface MarketUnsubscribeMessage {
  type: "unsubscribe";
  requestId: string;
  symbols: SymbolCode[];
}

export type MarketServerMessage =
  | MarketSubscriptionAck
  | MarketSnapshotMessage
  | MarketEventBatchMessage
  | MarketErrorMessage;

export interface MarketSubscriptionAck {
  type: "subscription";
  requestId: string;
  provider: Provider;
  symbols: SymbolCode[];
  timeframe: Timeframe;
  subscribedAt: IsoTimestamp;
}

export interface MarketSnapshotMessage {
  type: "snapshot";
  requestId: string;
  provider: Provider;
  timeframe: Timeframe;
  candlesBySymbol: Record<SymbolCode, Candle[]>;
  generatedAt: IsoTimestamp;
}

export interface MarketEventBatchMessage {
  type: "events";
  provider: Provider;
  sequence: number;
  events: NormalizedMarketEvent[];
  sentAt: IsoTimestamp;
}

export interface MarketErrorMessage {
  type: "error";
  requestId?: string;
  code: "invalid_subscribe_message" | "unsupported_symbol" | "unsupported_timeframe" | "internal_error";
  message: string;
  sentAt: IsoTimestamp;
}

export type StreamStatus = "idle" | "connecting" | "live" | "stale" | "error";
