# Bid-Ask Volume Profile — Implementation Plan (for Codex)

> **You are Codex.** This plan set is your single source of truth for this feature. It was written
> after reading the original design handoff AND the current codebase; all design decisions are
> folded into these documents. Where this plan and the current code differ on incidental details
> (a helper was renamed, a line moved), **verify against current code** and adapt the mechanical
> detail — but do not change the contracts defined here (schema, routes, event types, component
> boundaries) without flagging it.

## Reading order

| File | Contents |
|------|----------|
| `00_OVERVIEW.md` | (this file) Goal, decided constraints, target architecture, naming, glossary |
| `01_backend_streaming_and_subscription.md` | `alfaka.orderflow` package, quote cache, live 1m bin builder, Redis keys, WS publish, pinned subscription source, config/env |
| `02_storage_schema_and_eod_rollup.md` | ClickHouse DDL (final), EOD rollup job algorithm, CronJob manifest, backfill, retention |
| `03_api_and_ws_contracts.md` | REST endpoint signatures + exact payload shapes, WS event contract, stream_hub changes |
| `04_frontend.md` | chart type `bidask`, shared ladder utils/renderer, intraday tile panel, click linking, agent references |
| `05_footprint_removal.md` | Exact removal list (backend + frontend) for the legacy footprint feature |
| `06_rollout_validation_and_tests.md` | Implementation order with verification gates, test plan, deploy/migration notes |

Also read before coding: `AGENTS.md` (repo rules), `docs/CHART_DATA_REBUILD_PLAN.md` (chart data
contract you are extending), `docs/STRUCTURE_GUIDE.md` (file placement rules).

**Document precedence & AGENTS.md compliance:**

- This plan supersedes the original design handoff (`bid_ask_vp_handoff/`, since removed from the
  repo). References to "the handoff" in this plan set are historical rationale, not pointers to
  files you must read.
- AGENTS.md protects a fixed route list and forbids API/schema changes "during structure-only
  work". This is a feature change, and the user has **explicitly approved** the API contract
  changes in this plan: the three new `GET /api/charts/order-flow/*` routes, the removal of
  `GET /api/charts/footprint`, and the new `market_data.order_flow_profile_daily` table. None of
  the AGENTS.md-protected routes are touched. Per AGENTS.md documentation rules, update the
  affected docs in the same change (`05` §3, `06` §1 stage 8).
- Do not push or create commits beyond what your runner requires; do not touch order/KIS domains.

## What is being built

**Order Flow Profile** (screen label) / **Bid-Ask Volume Profile** (technical name): for each price
level, show the volume that executed against the ask (aggressive buying) vs against the bid
(aggressive selling), plus delta (= askVolume − bidVolume), POC, total delta, and imbalance
highlights. Side is **estimated** (Lee-Ready classification from L1 quotes; Alpaca provides no
aggressor flag) and the UI must say so.

Two views:

1. **View A — Daily (main chart panel).** New chart *type* `bidask` in the existing chart-type
   dropdown (Candle/Line/OHLC/Bid-Ask). Interval is locked to `1D`. Each x-slot = one session's full
   order-flow profile ladder, laid out side by side by date. Today's column fills live while the
   market is open.
2. **View B — Intraday (independent tile panel).** New panel kind `orderFlow` registered in
   `panelRegistry`. A single-profile viewer with two modes:
   - **LIVE** (today): trailing-window slider `1m / 10m / 1h / session`; current minute updates via
     WS; realtime L1 quote shown beside the ladder. If the market is closed, shows the most recent
     completed session.
   - **SELECTED** (a past date): a static full-day aggregate, slider locked to `day`.

## Decisions that are FIXED (user-confirmed — do not change)

1. Intraday = independent tile panel (not a below-pane). Single profile viewer.
2. Daily = main chart panel chart type `bidask`, interval locked to 1D.
3. **Panel linking (user-specified rule):** the chart panel and the orderFlow panel each have their
   *own* symbol setting. They are only linked when the symbols match. Concretely: while the chart
   panel shows 1D (candle/ohlc **or** bidask) and the user click-selects a day unit, an open
   orderFlow panel **with the same symbol** switches to SELECTED mode for that date. When the
   selection is cleared, the orderFlow panel returns to LIVE (or last completed session if closed).
   No permanent panel-to-panel link, no real-time symbol sync.
4. Order-flow columns in View A are click-selectable like candles and referenceable in the agent
   question box (new reference type).
5. MVP metrics: per-level bid/ask volume + per-level delta coloring (required), Total delta, POC,
   Imbalance highlights. Phase 2 (leave room, do NOT build): cumulative delta, value area 70%.
6. Regular session (`market_session = 'regular'`) only, both views. No historical intraday (past
   dates = daily aggregates only). The legacy "dig → footprint" interaction is removed.
7. Intraday live = **WS-native** (REST snapshot on connect + WS push). No polling, no derived-worker
   RPC for the live path.
8. Side classification = quote-based Lee-Ready (reuse existing `classify_trade_side`), labeled
   `estimated`. Realtime L1 quote shown in the intraday panel.
9. **Phase 1 symbols = NVDA, AMZN, MU, AAPL, GOOGL, pinned** via a new subscription source. The
   feature is **pin-only in the UI for MVP**: for any other symbol, both views render an explicit
   "not supported yet" empty state (see `04_frontend.md` §8).
10. The legacy footprint feature is **fully removed** in this rollout: the `footprint` interval
    option, `GET /api/charts/footprint`, the derived-worker `footprint` kind, and the dig→footprint
    semantic rendering. See `05_footprint_removal.md`.
11. **Every other existing chart feature must keep working**: the candle-based volume-profile
    overlay (`volume-profile` layer + `GET /api/charts/volume-profile-bins`), indicators, compare,
    candles, drawings, agent chart context. The VP overlay stays candle-estimated and untouched
    except where it can adopt shared client utilities without behavior change (`04_frontend.md` §9).

## Target architecture (goal state, not a diff)

```
Alpaca WS (trades+quotes; pinned 5 always subscribed via 'orderflow' cohort source)
      │
      ▼
market-ingestor → Kafka market.input.realtime.{trades,quotes}.v1        (unchanged)
      │
      ▼
market-processor (trades path)                    market-quote-processor (quotes path)
  ├─ existing candle/VP builders (unchanged)        └─ writes live:quote:{symbol} (unchanged)
  ├─ NEW OrderFlowBinBuilder (pinned symbols,                   ▲
  │    regular session only, $0.01 bins,                        │ read (cached ≤150ms)
  │    side via PinnedQuoteCache ────────────────────────────────┘
  ├─ NEW Redis HASH order-flow:{symbol}:live  (today's 1m bins, idempotent field writes)
  └─ NEW publish ORDER_FLOW_BINS_UPDATE via existing Redis pub/sub market.events (throttled)
                       │
                       ▼
api-server stream_hub → /ws/charts  (ORDER_FLOW_BINS_UPDATE delivered symbol-matched,
                                     interval-agnostic, droppable under backpressure)
                       │
      ┌────────────────┴───────────────────┐
      ▼                                    ▼
Intraday orderFlow tile panel        Main chart panel, chartType=bidask
  REST snapshot /api/charts/           REST /api/charts/order-flow/daily (past days)
  order-flow/intraday + WS live        + same WS event feeds today's live column
  (client window + price-step binning)  (client price-step binning)

EOD (weekday CronJob, 21:30 UTC): jobs/order-flow-daily-rollup
  reads trade_ticks + quote_ticks (regular session, streaming as-of merge)
  → INSERT market_data.order_flow_profile_daily  (permanent, $0.01 bins)
```

Key properties:

- **Atomic unit = 1-minute per-price-bin bucket with ask/bid/unknown split.** Daily = sum of a
  session's minute buckets (materialized once by the EOD job). Intraday windows = client-side
  trailing sums of minute buckets. No per-interval storage.
- **Storage:** 1m bins live in Redis for today only (expire/reset per session). Daily profiles are
  permanent in ClickHouse. No new durable 1m table in MVP.
- **Base tick:** backend always produces `$0.01` bins (`price_bin_size = 0.01`). All coarser price
  steps are client-side re-binning. This is symmetric with client-side time windowing (handoff D5).
- **No new Kafka topics.** Live fan-out uses the existing Redis pub/sub `market.events` channel
  that `publish_chart_event` already uses; daily uses REST from ClickHouse.
- The existing side-blind `VolumeProfileBinBuilder` / `volume-profile:{symbol}:1m:live` Redis key
  and all of its consumers are **left untouched** (existing-feature preservation). The order-flow
  builder is a new, parallel, pin-gated builder.

## Deviations from the handoff (approved)

The handoff (`02`, `03`) suggested extending the existing VP builder/Redis key in place. This plan
instead adds a **separate builder + separate Redis HASH key**, because (a) the existing ZSET key has
existing readers (`redis_provider.volume_profile_bins`) whose behavior must not change, (b) the
existing ZSET write pattern appends a new member per update (accumulating snapshots) which is the
wrong shape for idempotent minute-bin updates, and (c) $0.01 bins for all ~100 subscribed symbols
would be wasteful — pin-gating keeps memory bounded. The handoff's `VOLUME_PROFILE_BINS_UPDATE`
reserved WS type is left reserved; we introduce `ORDER_FLOW_BINS_UPDATE` because the payload is
side-split and the delivery rule differs (interval-agnostic).

## Canonical names (use exactly these everywhere)

| Thing | Name |
|---|---|
| Python package | `alfaka.orderflow` (`systems/market-data/shared/alfaka/orderflow/`) |
| Classification version string | `"orderflow-estimated-v1"` |
| Side classification label | `"estimated"` |
| Redis live key | `order-flow:{symbol}:live` (HASH; via new `RedisKeyBuilder.order_flow_live(symbol)`) |
| WS event type | `"ORDER_FLOW_BINS_UPDATE"` |
| ClickHouse table | `market_data.order_flow_profile_daily` |
| REST routes | `GET /api/charts/order-flow/daily`, `GET /api/charts/order-flow/intraday`, `GET /api/charts/order-flow/symbols` |
| Subscription cohort source | `"orderflow"` (`ORDER_FLOW_SOURCE`) |
| EOD job | `systems/market-data/jobs/order-flow-daily-rollup/main.py` |
| CronJob manifest | `infra/k8s/overlays/aws/cronjob-order-flow-daily-rollup.yaml` |
| Frontend chart type value | `"bidask"` (dropdown label `Bid/Ask`) |
| Frontend panel kind | `"orderFlow"` (registry title `오더플로우`, agentPanelType `"orderFlowProfile"`) |
| Frontend shared modules | `src/chart/orderFlow.ts` (types+transforms), `src/chart/orderFlowRender.ts` (canvas renderer), `src/chart/orderFlowClient.ts` (REST/WS client helpers) |
| Agent reference type | `"chart.orderFlow"` |
| Env vars | `ORDER_FLOW_PINNED_SYMBOLS`, `ORDER_FLOW_PRICE_BIN_SIZE` (default `0.01`), `ORDER_FLOW_QUOTE_REFRESH_MS` (default `150`), `ORDER_FLOW_PUBLISH_THROTTLE_MS` (default `250`), `ORDER_FLOW_LIVE_TTL_SECONDS` (default `86400`) |

`ORDER_FLOW_PINNED_SYMBOLS` default: `NVDA,AMZN,MU,AAPL,GOOGL`.

## Glossary

- **bin / level:** one price bucket. `price_bin` = bin center rounded to the bin size grid
  (`round(round(price / bin_size) * bin_size, 6)` — same rule the existing `VolumeProfileBinBuilder`
  uses).
- **ladder:** a normalized array of levels for one display column, plus `priceStep`, totals, POC,
  imbalance marks. The single input shape both renderers consume (handoff D4).
- **askVolume:** volume of trades classified `ask` (buyer lifted the offer). **bidVolume:** trades
  classified `bid` (seller hit the bid). **unknownVolume:** unclassifiable (no quote / exact mid).
- **delta:** `askVolume − bidVolume` (per level or per ladder).
- **regular session:** 09:30–16:00 US/Eastern, weekdays, minus the closed dates in
  `alfaka/alpaca/feed_profiles.py` (`market_session_for_datetime`).

## Definition of done (summary — full checklist in `06`)

- All 5 pinned symbols accumulate live 1m order-flow bins during regular hours and a daily row-set
  after each session; both views render them; footprint is gone; every command in the verification
  list of `06_rollout_validation_and_tests.md` §6 passes.
