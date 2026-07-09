# 01 — Backend: `alfaka.orderflow` package, streaming live path, subscription pin

Everything in this file happens under `systems/market-data/` unless stated otherwise.

## 1. New package: `systems/market-data/shared/alfaka/orderflow/`

Create the package with these modules. This package is the single home for order-flow domain logic;
the streaming processor, the EOD job, and the api-server all import from it.

### 1.1 `alfaka/orderflow/__init__.py`

Re-export the public names: `classify_trade_side`, `OrderFlowBinBuilder`, `PinnedQuoteCache`,
`merge_trades_with_quotes`, `pinned_symbols_from_env`, `ORDER_FLOW_CLASSIFICATION_VERSION`.

### 1.2 `alfaka/orderflow/classification.py`

Move (verbatim logic, same behavior) from `alfaka/serving/footprint.py`:

- `classify_trade_side(trade, quote) -> str` — returns `"ask" | "bid" | "unknown"`. Current logic
  (keep exactly): no quote → `unknown`; `price >= askPrice` → `ask`; `price <= bidPrice` → `bid`;
  else if both sides present and `ask >= bid`, compare to mid `(bid+ask)/2` (`> mid` → ask,
  `< mid` → bid, `== mid` → unknown); else `unknown`.
- The trade/quote normalization helpers it depends on (`normalize_trades`, `normalize_quotes`,
  timestamp parsing). Check the current `footprint.py` for the exact helper set and move what
  classification needs; drop the bucket-building helpers (they are replaced by `bins.py`/`rollup.py`).

Add:

- `ORDER_FLOW_CLASSIFICATION_VERSION = "orderflow-estimated-v1"`
- `ORDER_FLOW_SIDE_CLASSIFICATION = "estimated"`
- `merge_trades_with_quotes(trades_iter, quotes_iter) -> Iterator[tuple[trade, quote|None]]` — a
  **streaming** as-of merge: both iterators MUST be time-ordered; advance a single active-quote
  pointer to the latest quote with `quote_time <= trade_time`; yield `(trade, active_quote)`. This
  is the same forward-pointer join `aggregate_footprint_buckets` used, but as a generator over
  iterators (O(1) memory) so the EOD job can stream millions of quotes. It must accept a
  `initial_quote=None` kwarg so hourly-windowed callers can carry the last quote across windows.

Field-name note: trades/quotes may arrive in two shapes — serving-normalized camelCase
(`bidPrice`/`askPrice`, `price`, `size`, `timestamp`) and ClickHouse row snake_case. Standardize:
`classification.py` works on the camelCase shape; callers normalize first (rollup normalizes CH
rows; the streaming path already has camelCase from `normalize_trade`/`normalize_quote` in
`alfaka/streaming/transforms.py` — verify those output field names and match them).

### 1.3 `alfaka/orderflow/bins.py` — `OrderFlowBinBuilder`

Modeled on `VolumeProfileBinBuilder` (`alfaka/streaming/transforms.py`) but side-split, pin-gated,
session-gated, and minute-hash oriented.

```python
class OrderFlowBinBuilder:
    def __init__(self, price_bin_size: float = 0.01, pinned_symbols: frozenset[str] = ...):
        ...

    def update(self, trade: dict, side: str) -> dict | None:
        """Accumulate one classified trade. Returns the updated bin dict, or None if skipped."""
```

Rules:

- Skip (return `None`) when: `trade["symbol"] not in pinned_symbols`; `trade.get("marketSession")
  != "regular"`; `size <= 0` or missing price/timestamp.
- `minute` = floor of trade timestamp to the minute (reuse the same `floor_minute` helper the VP
  builder uses). `price_bin = round(round(price / bin_size) * bin_size, 6)`.
- Keyed accumulation on `(symbol, minute, price_bin)` with fields:
  `askVolume, bidVolume, unknownVolume, askTradeCount, bidTradeCount, unknownTradeCount` —
  incremented per `side` — plus `volume` (total) and `tradeCount` (total).
- Returned bin dict shape (this is what gets written to Redis and emitted over WS):

```python
{
  "eventType": "ORDER_FLOW_BIN",
  "eventMinute": "2026-07-09T14:31:00.000Z",   # ISO, minute floor, UTC
  "sessionDate": "2026-07-09",                  # US/Eastern trading date of the minute
  "symbol": "NVDA",
  "priceBin": 158.34,
  "priceBinSize": 0.01,
  "askVolume": 1200.0, "bidVolume": 800.0, "unknownVolume": 15.0,
  "askTradeCount": 42, "bidTradeCount": 31, "unknownTradeCount": 1,
  "volume": 2015.0, "tradeCount": 74,
  "sideClassification": "estimated",
  "classificationVersion": "orderflow-estimated-v1",
  "source": "alpaca", "feed": trade.get("feed") or "unknown",
  "marketSession": "regular",
  "updatedAt": now_iso,
}
```

- `sessionDate`: compute the US/Eastern date of `eventMinute` (use `zoneinfo.ZoneInfo("America/New_York")`;
  check `alfaka/alpaca/feed_profiles.py` for an existing ET helper and reuse it if present).
- Expose `current_session_date(symbol)` and `pop_finished_minutes(symbol, now_minute)` if useful,
  but keep the class minimal; eviction of old in-memory keys: on each `update`, drop in-memory
  entries whose `eventMinute` is older than 3 minutes (memory bound; Redis is the store of record).

### 1.4 `alfaka/orderflow/quote_cache.py` — `PinnedQuoteCache`

The trades processor pod does **not** consume the quotes topic (quotes are handled by the separate
quote-processor pod, which already writes `live:quote:{symbol}` to Redis — see
`write_quote_to_redis` in `alfaka/streaming/processor.py`, SET JSON with EXPIRE 300). The order-flow
path therefore classifies against a **short-lived cached read** of that key:

```python
class PinnedQuoteCache:
    def __init__(self, redis_client, redis_keys, refresh_ms: int = 150):
        ...
    def quote_for(self, symbol: str) -> dict | None:
        """Return the cached live quote for symbol, re-reading Redis when the cached copy
        is older than refresh_ms. Returns None on missing key / parse failure / Redis error."""
```

- Per-symbol cache entry: `(quote_dict, fetched_monotonic)`. On `quote_for`, if
  `monotonic() - fetched >= refresh_ms/1000`, GET `redis_keys.live_quote(symbol)`, JSON-parse,
  store. Any exception → keep old entry if fresh-ish (< 5s) else return `None` (classification then
  yields `unknown`, which is the honest answer).
- Verify the field names in the stored live quote (written by `write_quote_to_redis` from
  `normalize_quote` output) and make sure `classify_trade_side` reads the same names
  (`bidPrice`/`askPrice`). Add a tiny adapter here if they differ.
- Accuracy note (document in the module docstring): live classification can use a quote up to
  ~refresh_ms + write-latency stale. That is acceptable for the live view (labeled estimated); the
  EOD rollup recomputes daily profiles with an exact as-of join, so stored daily data is not
  affected by this cache.

### 1.5 `alfaka/orderflow/config.py`

```python
def pinned_symbols_from_env() -> frozenset[str]:
    # ORDER_FLOW_PINNED_SYMBOLS, default "NVDA,AMZN,MU,AAPL,GOOGL", upper-cased, stripped
def price_bin_size_from_env() -> float:      # ORDER_FLOW_PRICE_BIN_SIZE, default 0.01
def quote_refresh_ms_from_env() -> int:      # ORDER_FLOW_QUOTE_REFRESH_MS, default 150
def publish_throttle_ms_from_env() -> int:   # ORDER_FLOW_PUBLISH_THROTTLE_MS, default 250
def live_ttl_seconds_from_env() -> int:      # ORDER_FLOW_LIVE_TTL_SECONDS, default 86400
```

### 1.6 `alfaka/orderflow/rollup.py`

EOD aggregation logic — specified in `02_storage_schema_and_eod_rollup.md` §3. Lives here so the
job entrypoint stays thin per repo convention.

## 2. Redis key

`alfaka/common/redis_keys.py` — add to `RedisKeyBuilder`:

```python
def order_flow_live(self, symbol):
    return self.key(f"order-flow:{symbol}:live")
```

Key type: **HASH**. Field = `f"{eventMinute}|{priceBin:.2f}"` (e.g. `2026-07-09T14:31:00.000Z|158.34`).
Value = the bin dict JSON (compact separators, like existing writers). Idempotent overwrite per
(minute, bin) — this is why it is a HASH and not a ZSET like the legacy VP key.

## 3. Streaming processor wiring (`alfaka/streaming/processor.py`)

The processor's per-run state object (see `processor_runtime_config()` / the `state` passed into
`process_trade_live_path`) gains three members, constructed at startup **only when
`pinned_symbols_from_env()` is non-empty**:

- `state.order_flow_builder = OrderFlowBinBuilder(price_bin_size_from_env(), pinned_symbols)`
- `state.order_flow_quote_cache = PinnedQuoteCache(redis_client, redis_keys, quote_refresh_ms_from_env())`
- `state.order_flow_publish_state = {}`  # per-symbol throttle bookkeeping, see 3.2

### 3.1 Hook into `process_trade_live_path(trade, producer, redis_client, redis_keys, state, topics, ...)`

After the existing `profile_bin = state.profile_builder.update(trade)` /
`write_volume_profile_bin_to_redis(...)` lines (which stay **unchanged**), add:

```python
if state.order_flow_builder is not None and trade.get("symbol") in state.order_flow_builder.pinned_symbols:
    quote = state.order_flow_quote_cache.quote_for(trade["symbol"])
    side = classify_trade_side(trade, quote)
    of_bin = state.order_flow_builder.update(trade, side)
    if of_bin is not None:
        write_order_flow_bin_to_redis(redis_client, redis_keys, of_bin)
        maybe_publish_order_flow_event(redis_client, redis_keys, state, of_bin)
```

New module-level functions in `processor.py` (mirroring the style of
`write_volume_profile_bin_to_redis` / `publish_chart_event`):

```python
def write_order_flow_bin_to_redis(redis_client, redis_keys, of_bin):
    key = redis_keys.order_flow_live(of_bin["symbol"])
    field = f"{of_bin['eventMinute']}|{of_bin['priceBin']:.2f}"
    redis_client.hset(key, field, json.dumps(of_bin, ensure_ascii=False, separators=(",", ":")))
    redis_client.expire(key, live_ttl_seconds_from_env())
```

**Session rollover reset:** the builder tracks the last `sessionDate` it wrote per symbol; when a
trade produces a bin whose `sessionDate` differs from the previous one, `DEL` the hash key before
the first `HSET` of the new session (so yesterday's fields never leak into today's snapshot). Keep
that bookkeeping inside `write_order_flow_bin_to_redis` via a small per-symbol dict on `state`
(or a builder attribute) — implementer's choice, but it must be covered by a unit test.

### 3.2 Throttled WS publish — `maybe_publish_order_flow_event`

Do **not** publish one pub/sub event per trade (NVDA peaks at hundreds of trades/sec). Throttle per
symbol to at most one event per `ORDER_FLOW_PUBLISH_THROTTLE_MS` (default 250ms), and make each
event a **full replace of the current minute** so clients are idempotent:

```python
def maybe_publish_order_flow_event(redis_client, redis_keys, state, of_bin):
    sym = of_bin["symbol"]
    now = time.monotonic()
    entry = state.order_flow_publish_state.get(sym)  # {"lastPublish": float, "minute": str}
    minute_changed = entry is not None and entry["minute"] != of_bin["eventMinute"]
    throttled = entry is not None and (now - entry["lastPublish"]) * 1000 < publish_throttle_ms
    if throttled and not minute_changed:
        return
    if minute_changed:
        # flush the FINAL state of the previous minute first (read its bins from the builder
        # or Redis) so clients always end a minute with complete data
        publish_chart_event(redis_client, redis_keys,
                            order_flow_event(sym, entry["minute"], bins_for_minute(..., entry["minute"])))
    publish_chart_event(redis_client, redis_keys,
                        order_flow_event(sym, of_bin["eventMinute"], bins_for_minute(..., of_bin["eventMinute"])))
    state.order_flow_publish_state[sym] = {"lastPublish": now, "minute": of_bin["eventMinute"]}
```

`bins_for_minute(symbol, minute)` returns all bin dicts of that minute from the builder's in-memory
map (do not read Redis on the hot path). Event envelope built by a new
`order_flow_event(symbol, event_minute, bins)` in `alfaka/serving/dto.py` — exact shape in
`03_api_and_ws_contracts.md` §2.1. Publish goes through the existing `publish_chart_event` (Redis
pub/sub to `market.events` + `market.events:{symbol}`); **no Kafka publish** for order-flow bins.

Also: `market-quote-processor` needs no changes (it already maintains `live:quote:{symbol}`), and
the **legacy** `VolumeProfileBinBuilder` path stays byte-for-byte unchanged.

## 4. Subscription pin: the `orderflow` cohort source

Goal: the 5 pinned symbols subscribe `trades`+`quotes` all session, every session, regardless of any
chart being open, and survive the `ALPACA_MAX_TRADE_SYMBOLS` cap.

### 4.1 `alfaka/realtime/subscription_cohorts.py`

- Add constant `ORDER_FLOW_SOURCE = "orderflow"` next to `MANUAL_SOURCE` etc.
- Add `replace_order_flow_source(self, symbols: Iterable[str])` mirroring how the ranking/manual
  sources store their member sets (a `subscription:source:orderflow:symbols` SET via the existing
  `subscription_source_symbols(source)` key builder). Layers for this source: `{"trades", "quotes"}`.
- Extend `_collect_source_members` / `_write_aggregate_source_keys` /
  `_build_subscription_records` / `reason_for_sources` so `orderflow` members merge like other
  sources (study how `manual` is threaded through and copy that pattern; `manual` is the closest
  template since it is SET-based with explicit layers).

### 4.2 `pods/subscription-controller/main.py`

At the top of each reconcile loop (before `cohorts.reconcile()`), assert the pinned set:

```python
cohorts.replace_order_flow_source(sorted(pinned_symbols_from_env()))
```

Idempotent every 5s tick; if the env changes, the set follows on the next tick.

### 4.3 Cap priority — `alfaka/alpaca/websocket_collector.py`

`limit_realtime_symbols` ranks by `realtime_subscription_priority`, which ranks sources
(`active-chart` currently first). Give `orderflow` the **highest** source rank (before
`active-chart`) so the pinned 5 are never trimmed by the `ALPACA_MAX_TRADE_SYMBOLS` cap. Find the
source-rank mapping used by `realtime_subscription_priority` and insert `orderflow` at rank 0.
The existing "quotes-require-trades" enforcement needs no change (the source declares both layers).

### 4.4 Config surface

- `systems/market-data/config/market-data-request.json`: add an `orderFlow` block for
  documentation/config parity:
  `{"pinnedSymbols": ["NVDA","AMZN","MU","AAPL","GOOGL"], "priceBinSize": 0.01, "note": "phase-1 fixed pin; env ORDER_FLOW_PINNED_SYMBOLS wins"}`.
  Runtime reads env, not this file (consistent with how `ALPACA_MAX_TRADE_SYMBOLS` works).
- Env plumbing (all defaults live in code; set explicitly for prod clarity):
  - `docker-compose.yml`: add `ORDER_FLOW_PINNED_SYMBOLS`, `ORDER_FLOW_PRICE_BIN_SIZE: "0.01"` to
    `local-stream-processor` and `subscription-controller`; `ORDER_FLOW_PINNED_SYMBOLS` to
    `gops-backend` (REST validation, §03) .
  - `infra/k8s/base/app/configmap.yaml` (+ verify `overlays/aws/configmap-aws-patch.yaml` doesn't
    need a different value): same keys.
  - `.env.example` and `docs/ENVIRONMENT.md`: document all five `ORDER_FLOW_*` vars.

## 5. Unit tests for this file's scope

(Full test plan in `06`; write these alongside the code.)

- `systems/market-data/tests/test_orderflow_classification.py` — port the assertions of the current
  `test_footprint.py` (ask=10/bid=4/unknown=2 style case) against `classify_trade_side` +
  `merge_trades_with_quotes`, plus: quote-carry across window boundary via `initial_quote`.
- `systems/market-data/tests/test_orderflow_bins.py` — builder: side accumulation; pin gating;
  `marketSession != "regular"` skipped; minute rollover; `sessionDate` correctness around 00:00 UTC
  (a 19:59 ET trade and a 20:01 ET trade — the latter must be skipped as `after`, not misdated);
  Redis hash write field format; session-rollover `DEL`; publish throttle (fake clock: N updates in
  200ms → 1 event; minute change → previous-minute flush event emitted first).
- `systems/market-data/tests/test_orderflow_subscription.py` — `orderflow` source reconciles into
  `subscription:symbols` with layers trades+quotes; survives `ALPACA_MAX_TRADE_SYMBOLS=2` trimming
  (mirror the existing cap test in `test_market_data_hardening.py`).
