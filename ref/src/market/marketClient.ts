import type {
  MarketEventBatchMessage,
  MarketServerMessage,
  MarketSnapshotMessage,
  MarketSubscribeMessage,
  StreamStatus,
  SymbolCode,
  Timeframe
} from "../types/market";

export interface MarketClientOptions {
  symbols: SymbolCode[];
  timeframe: Timeframe;
  snapshotLimit: number;
  onSnapshot(message: MarketSnapshotMessage): void;
  onEvents(message: MarketEventBatchMessage): void;
  onStatus(status: StreamStatus): void;
  onError(message: string): void;
}

const BACKOFF_DELAYS = [500, 1000, 2000, 5000];

export class MarketClient {
  private socket: WebSocket | null = null;
  private closedByClient = false;
  private reconnectAttempt = 0;
  private staleTimer: number | null = null;

  constructor(private options: MarketClientOptions) {}

  connect(): void {
    this.closedByClient = false;
    this.options.onStatus("connecting");
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    this.socket = new WebSocket(`${protocol}://${window.location.host}/ws/market`);
    this.socket.addEventListener("open", () => this.subscribe());
    this.socket.addEventListener("message", (event) => this.handleMessage(event));
    this.socket.addEventListener("close", () => this.handleClose());
    this.socket.addEventListener("error", () => {
      this.options.onStatus("error");
      this.options.onError("Market stream connection error.");
    });
  }

  disconnect(): void {
    this.closedByClient = true;
    this.clearStaleTimer();
    this.socket?.close();
    this.socket = null;
  }

  private subscribe(): void {
    const message: MarketSubscribeMessage = {
      type: "subscribe",
      requestId: `sub-${Date.now()}`,
      symbols: this.options.symbols,
      timeframe: this.options.timeframe,
      includeTrades: false,
      includeQuotes: false,
      includeBars: true,
      snapshotLimit: this.options.snapshotLimit
    };
    this.socket?.send(JSON.stringify(message));
  }

  private handleMessage(event: MessageEvent): void {
    const message = JSON.parse(event.data) as MarketServerMessage;
    if (message.type === "snapshot") {
      this.reconnectAttempt = 0;
      this.options.onSnapshot(message);
      this.markLive();
    } else if (message.type === "events") {
      this.options.onEvents(message);
      this.markLive();
    } else if (message.type === "error") {
      this.options.onStatus("error");
      this.options.onError(message.message);
    }
  }

  private handleClose(): void {
    this.clearStaleTimer();
    if (this.closedByClient) return;
    this.options.onStatus("stale");
    const delay = BACKOFF_DELAYS[Math.min(this.reconnectAttempt, BACKOFF_DELAYS.length - 1)];
    this.reconnectAttempt += 1;
    window.setTimeout(() => this.connect(), delay);
  }

  private markLive(): void {
    this.options.onStatus("live");
    this.clearStaleTimer();
    this.staleTimer = window.setTimeout(() => this.options.onStatus("stale"), 10_000);
  }

  private clearStaleTimer(): void {
    if (this.staleTimer !== null) {
      window.clearTimeout(this.staleTimer);
      this.staleTimer = null;
    }
  }
}
