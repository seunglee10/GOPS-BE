import { ChevronDown, LoaderCircle, LogIn, Minus, Plus, Search, SendHorizontal } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { getSymbolMeta, normalizeSupportedSymbol, type SupportedSymbol, type WatchlistSymbol } from "@gops/chart-engine/symbols";
import { useAuth } from "../auth/AuthProvider";

type OrderSide = "buy" | "sell";
type OrderMarket = "overseas";
type OrderType = "limit" | "current";

type OrderFormState = {
  market: OrderMarket;
  symbol: string;
  side: OrderSide;
  orderType: OrderType;
  qty: string;
  price: string;
  exchange: string;
};

type OrderTicketProps = {
  activeSymbol: SupportedSymbol;
  chartSymbols: readonly WatchlistSymbol[];
  symbolOptions: readonly WatchlistSymbol[];
  onSymbolOptionsRequest: (query: string) => void;
};

type OrderSnapshot = {
  order_id: string;
  request_id: string;
  client_order_id: string;
  status: string;
  symbol?: string;
  side?: string;
  qty?: string;
  price?: string;
  reason?: string | null;
};

type OrderEvent = {
  event_id?: string;
  status: string;
  reason?: string | null;
  created_at?: string;
};

type OrderSocketPayload = {
  type: "snapshot" | "update" | "error";
  order?: OrderSnapshot;
  events?: OrderEvent[];
  detail?: string;
};

type OrderBalance = {
  currency?: string;
  exchange?: string;
  orderable_cash?: string | null;
  orderable_qty?: string | null;
};

const DEFAULT_FORM: OrderFormState = {
  market: "overseas",
  symbol: "AAPL",
  side: "buy",
  orderType: "limit",
  qty: "1",
  price: "145.00",
  exchange: "NASD"
};

const ORDER_SEARCH_RESULT_LIMIT = 8;
const SHORT_COMPANY_NAMES: Record<string, string> = {
  NVDA: "NVIDIA"
};

const sideLabels: Record<OrderSide, string> = {
  buy: "매수",
  sell: "매도"
};

const orderTypeLabels: Record<OrderType, string> = {
  limit: "지정가",
  current: "현재가"
};

const socketStateLabels: Record<"idle" | "open" | "closed", string> = {
  idle: "대기",
  open: "연결됨",
  closed: "종료"
};

function makeIdempotencyKey() {
  if (globalThis.crypto?.randomUUID) {
    return globalThis.crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function websocketUrl(orderId: string) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/ws/orders/${orderId}`;
}

function orderStatusLabel(status?: string): string {
  switch (status?.toLowerCase()) {
    case "accepted":
      return "접수";
    case "submitted":
      return "전송";
    case "filled":
      return "체결";
    case "rejected":
      return "거부";
    case "cancelled":
    case "canceled":
      return "취소";
    case "pending":
      return "대기";
    default:
      return status ? status.toUpperCase() : "주문 가능";
  }
}

function formatUsd(value: number): string {
  if (!Number.isFinite(value)) {
    return "-";
  }

  return new Intl.NumberFormat("ko-KR", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2
  }).format(value);
}

function formatCurrency(value: number, currency = "USD"): string {
  if (!Number.isFinite(value)) {
    return "-";
  }

  return new Intl.NumberFormat("ko-KR", {
    style: "currency",
    currency,
    maximumFractionDigits: 2
  }).format(value);
}

function formatPriceDisplay(value: number | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "-";
  }

  return new Intl.NumberFormat("ko-KR", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  }).format(value);
}

function parseFiniteNumber(value: string | number | null | undefined): number | undefined {
  if (value === null || value === undefined || value === "") {
    return undefined;
  }
  const parsed = typeof value === "number" ? value : Number(value.replace(/,/g, ""));
  return Number.isFinite(parsed) ? parsed : undefined;
}

function formatOrderAmount(qty: string, price: string, orderType: OrderType, currentPrice: number | undefined): string {
  const quantity = Number(qty);
  const orderPrice = orderType === "current" ? currentPrice : Number(price);
  if (!Number.isFinite(quantity) || typeof orderPrice !== "number" || !Number.isFinite(orderPrice)) {
    return "-";
  }

  return formatUsd(quantity * orderPrice);
}

function displayCompanyName(meta: Pick<WatchlistSymbol, "symbol" | "name">): string {
  return SHORT_COMPANY_NAMES[meta.symbol] ?? meta.name;
}

function formatInputNumber(value: number, fractionDigits: number): string {
  if (!Number.isFinite(value)) {
    return "";
  }
  if (fractionDigits === 0) {
    return String(Math.max(0, Math.round(value)));
  }
  return value.toFixed(fractionDigits).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
}

function formatPriceInput(value: number): string {
  return Number.isFinite(value) ? Math.max(0, value).toFixed(2) : "";
}

function normalizeDecimalText(value: string): string {
  return value.replace(/[^\d.]/g, "").replace(/(\..*)\./g, "$1");
}

function marketToExchange(market: string | undefined, fallback = "NASD"): string {
  switch (market?.trim().toUpperCase()) {
    case "NASDAQ":
      return "NASD";
    case "NYSE":
      return "NYSE";
    case "AMEX":
    case "ARCA":
      return "AMEX";
    default:
      return fallback;
  }
}

function dedupeSymbols(symbols: readonly WatchlistSymbol[]): WatchlistSymbol[] {
  const bySymbol = new Map<string, WatchlistSymbol>();
  for (const item of symbols) {
    const symbol = normalizeSupportedSymbol(item.symbol);
    if (symbol && !bySymbol.has(symbol)) {
      bySymbol.set(symbol, { ...item, symbol });
    }
  }
  return Array.from(bySymbol.values());
}

function resolveSymbolMeta(symbolValue: string, symbols: readonly WatchlistSymbol[]): WatchlistSymbol {
  const symbol = normalizeSupportedSymbol(symbolValue) ?? symbolValue.toUpperCase();
  const known = symbols.find((item) => item.symbol === symbol);
  const fallback = getSymbolMeta(symbol);
  if (known) {
    const name = known.name && known.name !== symbol ? known.name : fallback.name;
    const market = known.market && known.market !== "US" ? known.market : fallback.market;
    return { ...known, name, market };
  }
  return {
    symbol: fallback.symbol,
    name: fallback.name,
    market: fallback.market
  };
}

export function OrderTicket({
  activeSymbol,
  chartSymbols,
  symbolOptions,
  onSymbolOptionsRequest
}: OrderTicketProps) {
  const { authEnabled, user, loading: authLoading, login } = useAuth();
  const [form, setForm] = useState<OrderFormState>({ ...DEFAULT_FORM, symbol: activeSymbol });
  const [symbolSearchQuery, setSymbolSearchQuery] = useState("");
  const [symbolSearchOpen, setSymbolSearchOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | undefined>();
  const [order, setOrder] = useState<OrderSnapshot | undefined>();
  const [events, setEvents] = useState<OrderEvent[]>([]);
  const [balance, setBalance] = useState<OrderBalance | undefined>();
  const [balanceLoading, setBalanceLoading] = useState(false);
  const [balanceError, setBalanceError] = useState<string | undefined>();
  const [socketState, setSocketState] = useState<"idle" | "open" | "closed">("idle");
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const activeMeta = getSymbolMeta(activeSymbol);
    setForm((current) => ({
      ...current,
      symbol: activeSymbol,
      exchange: marketToExchange(activeMeta.market, current.exchange)
    }));
  }, [activeSymbol]);

  useEffect(() => {
    return () => {
      socketRef.current?.close();
    };
  }, []);

  const allSymbolOptions = useMemo(
    () => dedupeSymbols([...chartSymbols, ...symbolOptions]),
    [chartSymbols, symbolOptions]
  );

  const selectedSymbolMeta = useMemo(
    () => resolveSymbolMeta(form.symbol, allSymbolOptions),
    [allSymbolOptions, form.symbol]
  );

  const visibleChartSymbols = useMemo(() => {
    if (chartSymbols.length > 0) {
      return dedupeSymbols(chartSymbols);
    }
    return [selectedSymbolMeta];
  }, [chartSymbols, selectedSymbolMeta]);

  const visibleSearchOptions = useMemo(() => {
    const query = symbolSearchQuery.trim().toUpperCase();
    return dedupeSymbols(symbolOptions)
      .filter((item) => !query || item.symbol.includes(query) || item.name.toUpperCase().includes(query))
      .slice(0, ORDER_SEARCH_RESULT_LIMIT);
  }, [symbolOptions, symbolSearchQuery]);

  const dropdownChartSymbols = useMemo(() => {
    const otherChartSymbols = visibleChartSymbols.filter((item) => item.symbol !== form.symbol);
    return otherChartSymbols.length > 0 ? otherChartSymbols : visibleChartSymbols;
  }, [form.symbol, visibleChartSymbols]);

  const currentPrice = typeof selectedSymbolMeta.lastPrice === "number" && Number.isFinite(selectedSymbolMeta.lastPrice)
    ? selectedSymbolMeta.lastPrice
    : undefined;
  const currentPriceLabel = formatPriceDisplay(currentPrice);
  const selectedCompanyName = displayCompanyName(selectedSymbolMeta);
  const orderableCash = parseFiniteNumber(balance?.orderable_cash);
  const balanceCurrency = balance?.currency || "USD";
  const balanceLabel = balanceLoading
    ? "조회 중"
    : orderableCash !== undefined
      ? formatCurrency(orderableCash, balanceCurrency)
      : balanceError ? "조회 실패" : "-";

  useEffect(() => {
    const queryPrice = form.orderType === "current" ? currentPrice : Number(form.price);
    const balanceQueryPrice = typeof queryPrice === "number" && Number.isFinite(queryPrice) && queryPrice > 0
      ? formatPriceInput(queryPrice)
      : "0";
    const controller = new AbortController();
    const timeoutId = window.setTimeout(async () => {
      setBalanceLoading(true);
      setBalanceError(undefined);
      try {
        const params = new URLSearchParams({
          symbol: form.symbol,
          exchange: form.exchange,
          price: balanceQueryPrice
        });
        const response = await fetch(`/api/orders/balance?${params.toString()}`, {
          signal: controller.signal
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.detail ?? `잔고 조회 API 오류 ${response.status}`);
        }
        setBalance(payload as OrderBalance);
      } catch (caught) {
        if (!controller.signal.aborted) {
          setBalance(undefined);
          setBalanceError(caught instanceof Error ? caught.message : "잔고 조회에 실패했습니다.");
        }
      } finally {
        if (!controller.signal.aborted) {
          setBalanceLoading(false);
        }
      }
    }, 350);

    return () => {
      controller.abort();
      window.clearTimeout(timeoutId);
    };
  }, [currentPrice, form.exchange, form.orderType, form.price, form.symbol]);

  const selectOrderSymbol = (symbolValue: string) => {
    const symbol = normalizeSupportedSymbol(symbolValue);
    if (!symbol) {
      setError("유효한 종목 코드를 입력하세요.");
      return;
    }
    const meta = resolveSymbolMeta(symbol, allSymbolOptions);
    setForm((current) => ({
      ...current,
      symbol,
      exchange: marketToExchange(meta.market, current.exchange)
    }));
    setSymbolSearchQuery("");
    setSymbolSearchOpen(false);
    setError(undefined);
  };

  const updateTextField = (field: "qty" | "price", value: string) => {
    setForm((current) => ({ ...current, [field]: normalizeDecimalText(value) }));
  };

  const adjustPrice = (delta: number) => {
    setForm((current) => {
      const nextPrice = Math.max(0.01, (Number(current.price) || 0) + delta);
      return { ...current, price: formatPriceInput(nextPrice) };
    });
  };

  const adjustQuantity = (delta: number) => {
    setForm((current) => {
      const currentQuantity = Number(current.qty) || 0;
      const nextQuantity = Math.max(1, currentQuantity + delta);
      return {
        ...current,
        qty: formatInputNumber(nextQuantity, 0)
      };
    });
  };

  const applyBuyingPowerRatio = (ratio: number) => {
    setForm((current) => {
      const limitPrice = current.orderType === "limit" ? Number(current.price) : currentPrice;
      if (typeof limitPrice !== "number" || !Number.isFinite(limitPrice) || limitPrice <= 0) {
        return current;
      }
      if (orderableCash === undefined) {
        setError("잔고를 조회한 뒤 수량을 계산할 수 있습니다.");
        return current;
      }
      const rawQuantity = (orderableCash * ratio) / limitPrice;
      const nextQuantity = String(Math.max(1, Math.floor(rawQuantity)));
      return { ...current, qty: nextQuantity };
    });
  };

  const connectSocket = (orderId: string) => {
    socketRef.current?.close();
    const socket = new WebSocket(websocketUrl(orderId));
    socketRef.current = socket;
    setSocketState("idle");

    socket.onopen = () => setSocketState("open");
    socket.onclose = () => setSocketState("closed");
    socket.onerror = () => {
      setError("주문 스트림에 연결할 수 없습니다.");
      setSocketState("closed");
    };
    socket.onmessage = (event) => {
      const payload = JSON.parse(event.data) as OrderSocketPayload;
      if (payload.type === "error") {
        setError(payload.detail ?? "주문 스트림 오류가 발생했습니다.");
        return;
      }
      if (payload.order) {
        setOrder(payload.order);
      }
      if (payload.events) {
        setEvents(payload.events);
      }
    };
  };

  const submitOrder = async () => {
    if (authEnabled && !user) {
      login();
      return;
    }

    setSubmitting(true);
    setError(undefined);
    const idempotencyKey = makeIdempotencyKey();
    if (form.orderType === "current" && currentPrice === undefined) {
      setError("현재가를 확인할 수 없어 주문을 전송할 수 없습니다.");
      setSubmitting(false);
      return;
    }
    const submitPrice = form.orderType === "current" ? formatPriceInput(currentPrice ?? 0) : form.price;
    const quantity = Number(form.qty);
    const price = Number(submitPrice);
    if (!Number.isInteger(quantity) || quantity <= 0) {
      setError("해외주식 모의투자는 정수 수량만 주문할 수 있습니다.");
      setSubmitting(false);
      return;
    }
    if (!Number.isFinite(price) || price <= 0) {
      setError("지정가를 입력하세요.");
      setSubmitting(false);
      return;
    }
    try {
      const response = await fetch("/api/orders", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey
        },
        body: JSON.stringify({
          market: form.market,
          symbol: form.symbol,
          side: form.side,
          qty: form.qty,
          price: submitPrice,
          exchange: form.exchange,
          order_division: "00",
          actor_id: "gops-frontend",
          role: "trader"
        })
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.detail ?? (response.status === 401 ? "주문하려면 Google 로그인이 필요합니다." : `주문 API 오류 ${response.status}`));
      }
      setOrder(payload);
      setEvents([]);
      connectSocket(payload.order_id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "주문 요청에 실패했습니다.");
    } finally {
      setSubmitting(false);
    }
  };

  const estimatedAmount = formatOrderAmount(form.qty, form.price, form.orderType, currentPrice);

  return (
    <section className="order-ticket order-ticket-v2" data-order-side={form.side} aria-label="주문 패널">
      <div className="order-side-control order-segmented-control" role="group" aria-label="매수 매도 선택">
        <button className={form.side === "buy" ? "active" : ""} type="button" onClick={() => setForm((current) => ({ ...current, side: "buy" }))}>
          매수
        </button>
        <button className={form.side === "sell" ? "active" : ""} type="button" onClick={() => setForm((current) => ({ ...current, side: "sell" }))}>
          매도
        </button>
      </div>

      <div
        className="order-form-row order-symbol-row"
        onBlur={(event) => {
          const nextTarget = event.relatedTarget;
          if (!(nextTarget instanceof Node) || !event.currentTarget.contains(nextTarget)) {
            setSymbolSearchOpen(false);
          }
        }}
      >
        <span className="order-row-label">주문종목</span>
        <div className="order-symbol-picker">
          <button
            type="button"
            className="order-symbol-trigger"
            aria-expanded={symbolSearchOpen}
            aria-label="주문 종목 선택"
            onClick={() => {
              const nextOpen = !symbolSearchOpen;
              setSymbolSearchOpen(nextOpen);
              if (nextOpen) {
                onSymbolOptionsRequest(symbolSearchQuery);
              }
            }}
          >
            <span className="order-symbol-trigger-main">
              <span className="order-symbol-trigger-top">
                <strong>{form.symbol}</strong>
                <span>{selectedCompanyName}</span>
              </span>
            </span>
            <ChevronDown size={14} aria-hidden="true" />
          </button>

          {symbolSearchOpen && (
            <div className="order-symbol-dropdown" role="listbox" aria-label="주문 종목 선택">
              <div className="order-symbol-dropdown-section">
                {dropdownChartSymbols.map((item) => (
                  <button
                    key={`chart-${item.symbol}`}
                    type="button"
                    role="option"
                    aria-selected={item.symbol === form.symbol}
                    className={item.symbol === form.symbol ? "order-symbol-option active" : "order-symbol-option"}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => selectOrderSymbol(item.symbol)}
                  >
                    <strong>{item.symbol}</strong>
                    <span>{displayCompanyName(item)}</span>
                  </button>
                ))}
              </div>
              <div className="order-symbol-dropdown-divider" />
              <div className="order-symbol-dropdown-search">
                <button
                  type="button"
                  aria-label="주문 종목 검색"
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => onSymbolOptionsRequest(symbolSearchQuery)}
                >
                  <Search size={13} aria-hidden="true" />
                </button>
                <input
                  value={symbolSearchQuery}
                  placeholder="종목 검색"
                  aria-label="주문 종목 검색어"
                  onChange={(event) => {
                    const value = event.target.value.toUpperCase();
                    setSymbolSearchQuery(value);
                    onSymbolOptionsRequest(value);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                      event.preventDefault();
                      selectOrderSymbol(event.currentTarget.value);
                    }
                    if (event.key === "Escape") {
                      setSymbolSearchOpen(false);
                    }
                  }}
                />
              </div>
              <div className="order-symbol-dropdown-section">
                {visibleSearchOptions.map((item) => (
                  <button
                    key={`search-${item.symbol}`}
                    type="button"
                    role="option"
                    aria-selected={item.symbol === form.symbol}
                    className={item.symbol === form.symbol ? "order-symbol-option active" : "order-symbol-option"}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => selectOrderSymbol(item.symbol)}
                  >
                    <strong>{item.symbol}</strong>
                    <span>{displayCompanyName(item)}</span>
                  </button>
                ))}
                {visibleSearchOptions.length === 0 && (
                  <span className="order-symbol-empty">일치하는 종목이 없습니다</span>
                )}
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="order-form-stack">
        <div className="order-form-row">
          <span className="order-row-label">구매가격</span>
          <div className="order-segmented-control order-price-type-control" role="group" aria-label="구매 가격">
            {(["limit", "current"] as OrderType[]).map((type) => (
              <button
                key={type}
                type="button"
                className={form.orderType === type ? "active" : ""}
                onClick={() => setForm((current) => ({ ...current, orderType: type }))}
              >
                {orderTypeLabels[type]}
              </button>
            ))}
          </div>
        </div>

        <div className="order-form-row order-input-row">
          <span className="order-row-label">가격</span>
          <div className="order-input-stepper">
            <input
              inputMode="decimal"
              value={form.orderType === "current" ? currentPriceLabel : form.price}
              disabled={form.orderType !== "limit"}
              aria-label="주문 가격"
              onChange={(event) => updateTextField("price", event.target.value)}
            />
            <span className="order-input-suffix">USD</span>
            <div className="order-stepper-buttons" aria-label="가격 조정">
              <button type="button" disabled={form.orderType !== "limit"} aria-label="가격 낮추기" onClick={() => adjustPrice(-1)}>
                <Minus size={13} />
              </button>
              <span />
              <button type="button" disabled={form.orderType !== "limit"} aria-label="가격 올리기" onClick={() => adjustPrice(1)}>
                <Plus size={13} />
              </button>
            </div>
          </div>
        </div>

        <div className="order-form-row order-input-row">
          <span className="order-row-label">수량</span>
          <div className="order-input-stepper">
            <input
              inputMode="decimal"
              value={form.qty}
              placeholder="수량 입력"
              aria-label="주문 수량"
              onChange={(event) => updateTextField("qty", event.target.value)}
            />
            <span className="order-input-suffix">주</span>
            <div className="order-stepper-buttons" aria-label="수량 조정">
              <button type="button" aria-label="수량 줄이기" onClick={() => adjustQuantity(-1)}>
                <Minus size={13} />
              </button>
              <span />
              <button type="button" aria-label="수량 늘리기" onClick={() => adjustQuantity(1)}>
                <Plus size={13} />
              </button>
            </div>
          </div>
        </div>

        <div className="order-ratio-buttons" role="group" aria-label="주문 가능 금액 비율">
          <button type="button" onClick={() => applyBuyingPowerRatio(0.1)}>10%</button>
          <button type="button" onClick={() => applyBuyingPowerRatio(0.25)}>25%</button>
          <button type="button" onClick={() => applyBuyingPowerRatio(0.5)}>50%</button>
          <button type="button" onClick={() => applyBuyingPowerRatio(1)}>최대</button>
        </div>
      </div>

      <div className="order-summary-box order-summary-box-v2">
        <div>
          <span>총 주문 금액</span>
          <strong>{estimatedAmount}</strong>
        </div>
        <div className="order-buying-power-row">
          <span>주문 가능 금액</span>
          <strong>{balanceLabel}</strong>
        </div>
      </div>

      <button className="order-submit-button" type="button" disabled={submitting || authLoading} onClick={submitOrder}>
        {authEnabled && !user
          ? <LogIn size={14} />
          : submitting ? <LoaderCircle size={14} className="spin" /> : <SendHorizontal size={14} />}
        {authEnabled && !user ? "로그인" : submitting ? "전송 중" : `${sideLabels[form.side]} 주문 전송`}
      </button>

      {error && <div className="order-error">{error}</div>}

      {order && (
        <div className="order-status-box">
          <div>
            <span>상태</span>
            <strong>{orderStatusLabel(order.status)}</strong>
          </div>
          <div>
            <span>주문번호</span>
            <strong>{order.order_id}</strong>
          </div>
          <div>
            <span>스트림</span>
            <strong>{socketStateLabels[socketState]}</strong>
          </div>
        </div>
      )}

      {events.length > 0 && (
        <div className="order-event-list">
          {events.slice(-3).map((event, index) => (
            <div key={event.event_id ?? `${event.status}-${index}`} className="order-event-row">
              <strong>{event.status}</strong>
              <span>{event.reason ?? event.created_at ?? ""}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
