# GOPS Agent Orchestration

`agent-orchestration` owns the v1 stock-analysis agent runtime. It produces
analysis reports, layout proposals, market-event explanations, and notification
decisions. It does not execute orders or call account-control flows.

For team handoff, read `docs/AGENT_ARCHITECTURE.md` first, then
`docs/AGENT_INTEGRATION_FILE_GUIDE.md` for frontend/backend/AWS file
checklists. This README keeps the code-level ownership and run commands close
to the implementation.

## Runtime Units

| Runtime | Type | Required | Role |
| --- | --- | --- | --- |
| `agent-orchestrator` | pod | yes | HTTP compatibility endpoint for direct analysis and report lookup. |
| `agent-analysis-worker` | pod | yes | Consumes queued hot analysis requests and writes reports. |
| `agent-delivery-gateway` | pod | yes for async/SSE | Mirrors result events into report storage and Redis update channels. |
| `agent-intent-classifier` | pod | no | Optional cheap classifier endpoint for ambiguous multi-intent queries. |
| `deep-analysis-worker` | pod | no | Handles opt-in deep follow-up analysis outside the hot queue. |
| `event-detector` | pod | no | Reads market topics and emits unusual market events. |
| `notification-publisher` | pod | no | Publishes notification decisions to Redis/WebSocket consumers. |
| `graph-expansion-refresh` | job | no | Materializes GraphDB hints into Redis/ClickHouse cache. |
| smoke/eval jobs | job | no | Queue, store, graph, latency, fanout, retrieval, and grounding checks. |

All pods and jobs use the `gops-agent-orchestrator` image.

## Package Layout

```text
config/                 UI lexicon and fallback entity aliases
shared/gops_agents/
  contracts/            report, evidence, route, snapshot, runtime dataclasses
  query_understanding/  Korean-first entity/theme resolution
  intent_understanding/ content/UI intent decomposition and classifier adapters
  orchestration/        workflow, routing, timing, cache, tracing
  runtime/              admission, queues, workers, report store, delivery
  retrieval/            graph expansion, snapshots, cross signals, bulkheads
  providers/            news, ontology, macro adapters and provider caches
  roles/                logical role agents and AgentContext
  synthesis/            final answer synthesis
  events/               market event detection and notification publishing
pods/                   runtime entrypoint wrappers
jobs/                   smoke, refresh, benchmark, and eval entrypoints
tests/                  agent-owned unit and contract tests
```

`shared/gops_agents/orchestrator.py` is a compatibility import shim. The
workflow implementation lives under `shared/gops_agents/orchestration/`.

## Request Flow

```text
POST /api/agents/analyze
  -> AgentAnalysisRequestEnvelope
  -> agents.analysis-requests.v1
  -> agent-analysis-worker
  -> AgentOrchestrator
  -> query understanding + snapshots + synthesis
  -> report store
  -> agents.analysis-results.v1
  -> agent-delivery-gateway
  -> Redis report update channel
```

When `AGENT_ASYNC_ANALYSIS_ENABLED=false`, the backend can call
`agent-orchestrator` over HTTP for compatibility.

## Query Understanding

The hot path runs query understanding as a bounded fan-out:

- Korean entity/theme resolver
- deterministic content-task rules
- deterministic UI-task rules
- optional classifier pod or OpenAI classifier

The merged route mode is one of:

```text
analysis
ui_layout
hybrid
clarify
```

Company and theme resolution is catalog-based. `config/entity-aliases.seed.json`
and seed constants are bootstrap fallback data, not the operational source of
truth.

## Provider Status

- News uses Redis/ClickHouse cached news intelligence. Alpaca direct fallback is
  disabled unless explicitly enabled.
- Ontology uses `GraphDBOntologyProvider` and returns relationship snapshots.
- Macro intentionally remains an empty provider adapter in v1.
- Graph-aware retrieval reads Redis first and ClickHouse second when enabled.
- Cross-signal synthesis is feature-flagged.

Current implementation still imports `alfaka.*` helpers from
`systems/market-data/shared` for Kafka IO, ClickHouse/Redis providers, news
normalization, and optional fallback paths.

## Kafka Topics

```text
agents.market-events.v1
agents.analysis-requests.v1
agents.deep-analysis-requests.v1
agents.analysis-results.v1
agents.query-understanding-events.v1
agents.notification-decisions.v1
agents.dlq.v1
```

## Report Storage

Runtime report storage uses `ReportStore`.

Default Redis keys and channels:

```text
agent:report:{analysisId}
agent:report:latest:{SYMBOL}
agent:report:latest
agent:request:idempotency:{userHash}:{keyHash}
agent.reports
agent.reports:{analysisId}
```

Redis failures should fail open for report persistence so analysis generation is
not blocked by latest-report storage. Long-term Postgres history is not part of
the v1 agent runtime.

## Important Env

```text
AGENT_ASYNC_ANALYSIS_ENABLED
AGENT_SHARED_REPORT_STORE_ENABLED
AGENT_ANALYSIS_QUEUE_BACKEND
AGENT_REPORT_STORE_BACKEND
AGENT_REPORT_TTL_SECONDS
AGENT_REPORT_STREAM_REDIS_ENABLED
AGENT_ANALYSIS_REQUESTS_TOPIC
AGENT_DEEP_ANALYSIS_REQUESTS_TOPIC
AGENT_ANALYSIS_RESULTS_TOPIC
AGENT_QUERY_UNDERSTANDING_EVENTS_TOPIC
AGENT_NOTIFICATION_DECISIONS_TOPIC
AGENT_MARKET_EVENTS_TOPIC
AGENT_DLQ_TOPIC
AGENT_DEEP_ANALYSIS_ENABLED
KAFKA_BOOTSTRAP_SERVERS
REDIS_URL
CLICKHOUSE_HTTP_URL
GRAPHDB_SPARQL_URL
OPENAI_API_KEY
```

Use `docs/ENVIRONMENT.md` for the repo-wide env contract and
`docs/AGENT_ARCHITECTURE.md` for the agent handoff contract.

## Validation

Unit tests:

```sh
.venv/bin/python -m unittest discover -s systems/agent-orchestration/tests -p 'test_agent_orchestration.py'
```

Backend bridge tests:

```sh
.venv/bin/python -m unittest discover -s systems/api-server/tests -p 'test_agent_routes.py'
```

Smoke jobs:

```sh
PYTHONPATH=systems/agent-orchestration/shared:systems/market-data/shared \
  .venv/bin/python systems/agent-orchestration/jobs/agent-queue-smoke/main.py

PYTHONPATH=systems/agent-orchestration/shared:systems/market-data/shared \
  .venv/bin/python systems/agent-orchestration/jobs/report-store-smoke/main.py

PYTHONPATH=systems/agent-orchestration/shared:systems/market-data/shared \
  .venv/bin/python systems/agent-orchestration/jobs/graph-expansion-smoke/main.py
```

AWS acceptance requires a full async round trip:

```text
API 202 -> Kafka request -> worker report -> Redis store -> GET report -> SSE update
```
