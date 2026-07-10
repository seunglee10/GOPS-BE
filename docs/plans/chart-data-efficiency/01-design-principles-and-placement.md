# 01. Design Principles and Placement

## Cost model

Every calculation and copy is classified by the variable that drives its cost:

| Cost curve | Valid use | Invalid use |
| --- | --- | --- |
| Market activity (`trades + quotes`) | canonical ingestion, side classification, short-lived tick evidence | cache writes or derived persistence for every tick when no reader needs every update |
| Active symbol/time (`symbols * intervals * seconds`) | throttled live candle state, WebSocket delivery | 250 ms recovery scans or duplicate REST polling while events are healthy |
| User request (`requests * range * layers`) | optional indicators and candle volume profile | permanent streaming calculation for every symbol and parameter combination |
| Batch/session (`symbols * sessions`) | exact EOD order-flow rollup, retention compaction | live UI refresh |
| Bounded constant (`shards * windows`) | S3 realtime flush and health writes | symbol-keyed empty/small object creation |

The selected layer minimizes the dominant variable without hiding required work. A market-rate
classification cannot become constant without losing facts; a market-rate Redis write with no
reader can and should become zero.

## Ownership rules

1. **Facts are normalized once.** Alpaca envelopes enter Kafka keyed by symbol. Kafka retention is
   transport/replay protection, not the query database.
2. **Shared realtime state is stream-computed.** The processor may update in-memory state per event,
   but persistent writes/publishes must be time- or session-bounded and all in-memory collections
   must have an explicit horizon.
3. **Parameterized chart data is request-computed on the server.** The API uses shared calculation
   modules and a canonical candle facade. The browser formats and renders; it does not own the only
   implementation of a financial calculation.
4. **Session-final truth is batch-computed.** Daily order flow is immutable after rollup and is read
   from ClickHouse.
5. **Redis is disposable.** Keys require versioned names, normalized symbol/interval identity, TTL,
   cardinality caps, and a command budget test.
6. **ClickHouse stores queryable canonical or bounded intermediate data.** Request-hash artifacts
   with no independent reader do not qualify.
7. **S3 stores rebuild evidence with bounded object creation.** Realtime files are grouped by
   time/shard; one-off backfills keep request-scoped deterministic objects and manifests.
8. **The frontend caches only presentation state.** Cache identity includes every result-affecting
   parameter; maps use LRU/size bounds; a WS session has one recovery path.

## Read contract rules

- Public API route and response schemas are the frontend and external-consumer contracts.
- Internal callers do not bypass the service behind a route to call Alpaca, ClickHouse, or Redis
  directly unless they are the storage adapter implementing that service.
- Redis keys follow `gops:market:{purpose}:v{N}:...`; compatibility readers are time-boxed and emit
  a counter so removal is observable.
- Kafka topic inventories have one generated/source file and an equality test for rendered copies.
- ClickHouse local and K8s DDL copies are byte-equivalent except a generated header, enforced in CI.
- S3 layout versions are explicit in the prefix. Readers deploy before writers and dual-read during
  migration; writers never silently repurpose a v1 prefix.
- Aggregation rules that currently run in the browser remain shared TypeScript modules with fixture
  contracts. No new backend interval endpoint is added until an actual non-browser consumer needs it.

## Target placement table

| Data | Current placement | Decision | Target read contract |
| --- | --- | --- | --- |
| Raw Alpaca envelopes | Kafka + raw S3 | Keep, but raw S3 becomes 30-day sharded archive | internal replay tooling only; never normal chart read |
| Trade/quote ticks | raw/layer Kafka + ClickHouse; compose also final S3 | Keep Kafka; keep ClickHouse 21 days; remove processed final tick S3 | rollup/verify adapters read ClickHouse; repair reads raw S3 explicitly |
| Closed OHLCV candles | Kafka + Redis recent + ClickHouse + S3 final | Keep all three copies; each has a distinct hot/query/rebuild reader | existing `/api/charts/candles` through canonical facade |
| Fixed SMA 5/20/60 | stream state + candle rows | Keep with candle for compatibility/default readership; bound state to 60 and use rolling sums | candle response fields remain unchanged |
| Live candles | processor memory + Redis + pub/sub + live Kafka | Keep memory/Redis/pub-sub; remove unread live Kafka producer [CONTRACT-CHANGE CC-2] | `/ws/charts`, startup/recovery Redis snapshot |
| Candle volume profile | API/worker calculation + Redis + ClickHouse artifact; old tick VP Redis/table also exists | Remove old tick VP; calculate on request in shared server module; Redis TTL cache only [CONTRACT-CHANGE CC-1/CC-3] | existing volume-profile route and agent context facade |
| Optional indicators | API inline calculation + Redis; worker contract says otherwise | Keep request-time server compute, move to same derived service as VP; Redis TTL cache only [CONTRACT-CHANGE CC-3] | existing indicators route |
| Order-flow live minute | stream compute + Redis minute blobs + pub/sub | Keep; add bounded quote-cache miss recovery and remove legacy hash fallback after drain [CONTRACT-CHANGE CC-6] | existing intraday route + `ORDER_FLOW_BINS_UPDATE` |
| Order-flow daily | ClickHouse batch rollup | Keep unchanged | existing daily route and agent-context include |
| Chart-derived artifacts | ClickHouse TTL table + Redis + Kafka worker | Remove after inline shadow/equivalence and retention drain [CONTRACT-CHANGE CC-3] | no replacement artifact API; existing calculation routes remain |
| Compare series | direct Alpaca + Redis response cache | Move to canonical candle facade; Alpaca remains bounded fill-of-last-resort | existing compare route/response unchanged |
| Frontend 10m/1h order-flow buckets | client aggregation of canonical minute blobs | Keep as presentation aggregation; fix targeted cache identity | shared `orderFlow.ts` fixture contract |
| Frontend request/candle caches | unbounded maps with short TTL fields | Keep caches but enforce complete keys, LRU bounds, and expired sweep | browser-internal only |

## Why the mixed model is now intentional

The target is not "everything in Kafka" or "everything on request." It uses one rule per readership:

- fixed, widely shared, continuously visible values may be computed in the stream;
- arbitrary visible-range/layer calculations scale with actual requests and belong behind the API;
- exact session-final summaries belong in batch;
- the browser may aggregate already-delivered minute data for display, but the algorithm and fixture
  contract must be reusable and documented.

This preserves current pixels while removing accidental persistence and making every read path
available outside the frontend through an existing route.

## Rejected global alternatives

| Alternative | Rejection reason |
| --- | --- |
| Stream every indicator/VP for every symbol | Cost grows with market universe even when nobody views it; parameters/ranges are unbounded |
| Compute every derived series in the browser | Calculation becomes inaccessible to existing server consumers and duplicates across clients |
| Persist every request result in ClickHouse | Request hashes create a write-heavy cache table with no durable business reader |
| Use Redis as complete chart history | Memory cost and eviction couple hot delivery to durable correctness |
| Remove S3 or ClickHouse candle copies | Their rebuild and query responsibilities are distinct and currently exercised |
| Add analysis-engine routes now | There is no consumer or stable analysis contract; existing clean routes are sufficient preparation |
| Tune chart axes during data refactor | No evidence requires it and visual identity is a hard constraint |
