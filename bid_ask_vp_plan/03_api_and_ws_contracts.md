# 03 — API & WS contracts

api-server code lives under `systems/api-server/pods/api-server/gops-backend/app/`.

## 1. REST — three new endpoints

Register in `app/market_data/query/routes.py`, implement in
`app/market_data/query/service.py` (`MarketDataQueryService`), following the existing conventions:
`from`/`to` aliases, `normalize_market_symbol`, `ValueError→400`, `LookupError→404`, provider
failure→503, public (no auth) like the other chart routes. **No derived-worker RPC** — these read
ClickHouse/Redis directly through the provider.

### 1.1 `GET /api/charts/order-flow/symbols`

```python
@router.get("/api/charts/order-flow/symbols")
def order_flow_symbols() -> dict[str, Any]
```

Response:

```json
{ "symbols": ["AAPL", "AMZN", "GOOGL", "MU", "NVDA"], "priceBinSize": 0.01,
  "sideClassification": "estimated", "classificationVersion": "orderflow-estimated-v1" }
```

Source: `pinned_symbols_from_env()` + `price_bin_size_from_env()` (import from `alfaka.orderflow`).
The frontend uses this for the panel's symbol selector and for pin-gating both views.

### 1.2 `GET /api/charts/order-flow/daily`

```python
@router.get("/api/charts/order-flow/daily")
def order_flow_daily(
    symbol: str = Query(min_length=1, max_length=12),
    from_date: str = Query(alias="from"),          # YYYY-MM-DD (session_date, inclusive)
    to_date: str = Query(alias="to"),              # YYYY-MM-DD (inclusive)
    limit_days: int = Query(default=60, ge=1, le=250, alias="limitDays"),
) -> dict[str, Any]
```

Service `order_flow_daily(...)`:

- Validate symbol is pinned; if not → return `200` with `{"dataStatus": "unsupported",
  "supportedSymbols": [...] , "days": []}` (not an error — the frontend renders the unsupported
  state from this).
- Query (via a new provider method `order_flow_daily_profiles(symbol, from_date, to_date, limit)`
  on the ClickHouse provider — follow how `footprint_ticks` was structured on
  `alfaka/serving/clickhouse_provider.py` and place the new method there):

```sql
SELECT session_date, price_bin, price_bin_size,
       ask_volume, bid_volume, unknown_volume,
       ask_trade_count, bid_trade_count, unknown_trade_count,
       trade_count, volume
FROM market_data.order_flow_profile_daily FINAL
WHERE symbol = {symbol} AND session_date >= {from} AND session_date <= {to}
ORDER BY session_date ASC, price_bin ASC
```

- Group rows by `session_date` (cap at the most recent `limitDays` dates), compute per-day totals
  server-side (cheap sums), and return:

```json
{
  "symbol": "NVDA",
  "priceBinSize": 0.01,
  "sideClassification": "estimated",
  "classificationVersion": "orderflow-estimated-v1",
  "marketSession": "regular",
  "from": "2026-06-01", "to": "2026-07-09",
  "dataStatus": "ready",              // "ready" | "empty" | "unsupported"
  "days": [
    {
      "sessionDate": "2026-07-08",
      "totals": { "askVolume": 1.2e7, "bidVolume": 1.1e7, "unknownVolume": 3.1e5,
                  "delta": 1.0e6, "tradeCount": 812345, "volume": 2.33e7 },
      "levels": [
        { "priceBin": 158.34, "askVolume": 5200.0, "bidVolume": 4100.0, "unknownVolume": 40.0,
          "askTradeCount": 210, "bidTradeCount": 180, "unknownTradeCount": 2 }
      ]                                 // ascending priceBin
    }
  ]
}
```

POC / per-level delta / imbalance are **not** computed server-side — client `orderFlow.ts` utils
derive them (handoff D4/D5). Today's date is never in this response (it isn't in the table until
EOD); View A merges today from the intraday path client-side.

Caching: none in MVP (5 symbols, small payloads, immutable-once-written). Note this in the service
docstring; if needed later, a short Redis cache keyed
`chart:order-flow-daily:{symbol}:{from}:{to}` is the reserved design.

### 1.3 `GET /api/charts/order-flow/intraday`

```python
@router.get("/api/charts/order-flow/intraday")
def order_flow_intraday(symbol: str = Query(min_length=1, max_length=12)) -> dict[str, Any]
```

Service `order_flow_intraday(symbol)`:

- Pin-validate as in 1.2 (`dataStatus: "unsupported"` shape with `"minutes": []`).
- `HGETALL` `RedisKeyBuilder().order_flow_live(symbol)` (add a provider/redis-provider accessor
  next to `redis_provider.volume_profile_bins` — e.g. `redis_provider.order_flow_live_bins(symbol)`
  returning parsed bin dicts).
- Keep only fields whose bin `sessionDate` equals the **current ET trading date**; group by
  `eventMinute` ascending. (After the processor's rollover-DEL this is belt-and-suspenders.)
- Read the live L1 quote from `live:quote:{symbol}` (there is an existing accessor —
  the stream hub reads it; check `redis_provider` for `live_quote`) and pass it through.

```json
{
  "symbol": "NVDA",
  "sessionDate": "2026-07-09",
  "priceBinSize": 0.01,
  "sideClassification": "estimated",
  "classificationVersion": "orderflow-estimated-v1",
  "marketSession": "regular",
  "dataStatus": "ready",              // "ready" | "empty" | "unsupported"
  "minutes": [
    { "eventMinute": "2026-07-09T13:31:00.000Z",
      "bins": [ { "priceBin": 158.34, "askVolume": 1200.0, "bidVolume": 800.0,
                  "unknownVolume": 15.0, "askTradeCount": 42, "bidTradeCount": 31,
                  "unknownTradeCount": 1 } ] }
  ],
  "liveQuote": { "bidPrice": 158.33, "askPrice": 158.35, "bidSize": 4.0, "askSize": 7.0,
                 "timestamp": "2026-07-09T13:31:22.114Z" }   // or null
}
```

`dataStatus: "empty"` when the hash is empty/absent (e.g., market closed and key expired) — the
frontend then falls back to the latest daily profile (see `04` §6 LIVE-mode-closed behavior).

## 2. WS event — `ORDER_FLOW_BINS_UPDATE`

### 2.1 Envelope builder — `alfaka/serving/dto.py`

Add `order_flow_event(symbol, event_minute, bins)` next to `volume_profile_event` (which stays,
unused, untouched):

```python
{
  "type": "ORDER_FLOW_BINS_UPDATE",
  "eventId": f"delta/ORDER_FLOW_BINS_UPDATE/{symbol}/1m/{event_minute}",
  "cursor": f"v1:{symbol}:orderflow:{event_minute}",
  "symbol": symbol,
  "interval": "1m",
  "source": "alpaca",
  "feed": bins[0].get("feed", "unknown") if bins else "unknown",
  "data": {
    "symbol": symbol,
    "eventMinute": event_minute,
    "sessionDate": bins[0]["sessionDate"] if bins else None,
    "priceBinSize": 0.01,
    "sideClassification": "estimated",
    "classificationVersion": "orderflow-estimated-v1",
    "marketSession": "regular",
    "bins": [ /* full current state of ALL bins of this minute; same per-bin fields as REST */ ],
    "updatedAt": now_iso,
  },
}
```

**Semantics: full minute replace.** A client that receives this event replaces its entire entry for
`eventMinute` with `data.bins`. Events are throttled (≥250ms apart per symbol) and a final flush is
sent when the minute rolls over (`01` §3.2), so the last event per minute is always complete.

### 2.2 Delivery — `app/market_data/realtime/stream_hub.py`

Two exact edits:

1. `should_deliver_to_session`: deliver `ORDER_FLOW_BINS_UPDATE` **symbol-matched,
   interval-agnostic** (same branch as `LIVE_TRADE_UPDATE`/`LIVE_QUOTE_UPDATE`):

   ```python
   if event.get("type") in {"LIVE_TRADE_UPDATE", "LIVE_QUOTE_UPDATE", "ORDER_FLOW_BINS_UPDATE"}:
       return event.get("symbol") == session.symbol
   ```

   Rationale: View A's socket runs `interval=1D`, the intraday panel's runs `interval=1m`; both must
   receive the same event.
2. `StreamSession._drop_one_droppable_update`: add `"ORDER_FLOW_BINS_UPDATE"` to the droppable-type
   set (it is a full-replace event; dropping an intermediate one loses nothing — the next event or
   the minute-flush restores the state).

`event_marker` dedup: the marker tuple includes `eventId`/`cursor`, which change per publish
(`updatedAt` in data also varies) — no change needed, but confirm consecutive distinct events
aren't accidentally deduped (cursor is minute-stable; eventId is minute-stable too — **fix:**
include `data.updatedAt` in `eventId`, i.e.
`f"delta/ORDER_FLOW_BINS_UPDATE/{symbol}/1m/{event_minute}/{updated_at}"`, so the marker differs
per publish. Apply this in the dto builder above.)

### 2.3 Client subscription model (unchanged)

`/ws/charts?symbol={S}&interval={I}` per connection, no in-band protocol. The intraday panel opens
its own socket with `interval=1m` and ignores candle-type events; the chart panel in `bidask` mode
already holds a `1D` socket. Heartbeats/reconnect behavior unchanged.

## 3. Agent integration

- `GET /api/agent/context/chart` (`service.agent_chart_context`): when the requested symbol is
  pinned, extend the `include` vocabulary with `orderFlowDaily` (recent 5 sessions of daily
  profiles, totals only — no levels — to keep the context small). Optional-but-cheap; implement as
  a follow-on include flag defaulting OFF so existing consumers see no change.
- `POST /api/agents/analyze` needs **no backend change**: `references` passes through verbatim
  (`AgentAnalysisRequest` is `extra="allow"`), and the new frontend `chart.orderFlow` reference
  rides that path. Verify the analysis cache key folds `references` (the docs say selected
  candle/range references are folded in — the new type gets that for free since keying is on the
  payload).

## 4. AGENTS.md route contract

`AGENTS.md` "API Rules" lists routes that must be preserved — none are touched. The three new
routes are additive. Update the route inventory in `docs/CHART_DATA_REBUILD_PLAN.md` (add the three
order-flow routes; remove `GET /api/charts/footprint` per `05`).

## 5. Tests for this file's scope

`systems/api-server/tests/test_order_flow_query.py` (mirror the fake-provider style of
`test_market_data_query.py`):

- `symbols` endpoint returns env-configured pins;
- `daily`: grouping by session_date, totals math, `FINAL` in the SQL the fake captures, `limitDays`
  cap, unsupported symbol → `dataStatus:"unsupported"`, empty range → `"empty"`;
- `intraday`: HGETALL parsing, stale-sessionDate fields filtered, liveQuote pass-through, empty →
  `"empty"`;
- stream_hub: `should_deliver_to_session` delivers `ORDER_FLOW_BINS_UPDATE` to a `1D` session and a
  `1m` session of the same symbol, not to another symbol; backpressure: with a full queue, an
  order-flow event is droppable and never triggers the slow-client ERROR replacement alone.
