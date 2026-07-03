# GOPS Agent Orchestration

`agent-orchestration` owns the v1 role-based stock analysis agent runtime.

## Runtime Units

| Runtime | Type | Role |
| --- | --- | --- |
| `agent-orchestrator` | pod | Compatibility HTTP endpoint for direct analysis and report lookup. |
| `agent-analysis-worker` | pod | Consumes queued analysis requests and writes shared reports. |
| `agent-intent-classifier` | pod | Optional cheap classifier endpoint for ambiguous multi-intent query decomposition. |
| `deep-analysis-worker` | pod | Consumes opt-in deep analysis requests separately from the hot queue. |
| `agent-delivery-gateway` | pod | Mirrors analysis result events into the shared report store and Redis report update channels. |
| `event-detector` | pod | Reads market Kafka topics and emits unusual market events. |
| `notification-publisher` | pod | Publishes notification decisions to Redis/WebSocket consumers. |
| `graph-expansion-refresh` | job | Builds graph expansion hints from GraphDB and writes Redis/ClickHouse cache. |
| `agent-queue-smoke` | job | Checks request envelope, queue adapter, worker, and report-store round trip. |
| `report-store-smoke` | job | Checks shared report storage and idempotency mapping lookup. |
| `graph-expansion-smoke` | job | Checks graph expansion cache lookup and degraded miss behavior. |
| `retrieval-latency-benchmark` | job | Records p50/p95 stage latency for a representative query set. |
| `fanout-policy-benchmark` | job | Compares bounded related-symbol fanout values against latency/fanout metrics. |
| `retrieval-quality-eval` | job | Scores relation recall and provider evidence precision for golden queries. |
| `answer-grounding-eval` | job | Scores whether final-answer citations/text are grounded in provider evidence. |

## Logical Agents

The v1 logical agents run inside the `agent-orchestrator` pod:

- chart agent
- news agent
- macro agent
- ontology agent
- market summary agent
- unusual event explainer agent
- verification/guardrail agent
- notification decision agent
- layout agent

## Query Understanding

The hot request path decomposes the user query before route planning. It is a
parallel fan-out, not a single intent router:

```text
Kafka agents.analysis-requests.v1
  -> agent-analysis-worker
  -> normalize_request
  -> parallel query understanding
       - Korean entity/theme resolver
       - deterministic content-task rules
       - deterministic UI-task rules
       - optional cheap classifier pod/LLM
  -> merged routeMode: analysis | ui_layout | hybrid | clarify
  -> role agents and/or UIAgent
  -> agents.analysis-results.v1
  -> agents.query-understanding-events.v1
```

`shared/gops_agents/query_understanding/` owns Korean-first entity resolution.
The hot path is catalog-based rather than a hard-coded company/theme list:

- `catalog.py` loads company and theme entities from `market_data.symbols`,
  GraphDB theme relationships, and the reviewed alias artifact.
- `alias_index.py` builds an in-memory exact/compact/choseong/jamo/fuzzy index
  for low-latency lookup across large symbol universes.
- `entity_resolver.py` resolves both company entities and theme entities.
  Company results can override the current chart symbol; theme results feed
  `newsTopic` and bounded `newsSymbols` fanout.
- `config/entity-aliases.seed.json` and seed constants are fallback bootstrap
  data only. They are not the operational source of truth.

The resolver maps ticker, English/Korean company aliases, compact mixed text
such as `apple뉴스알려줘`, Hangul typos such as `얘플`, and long-enough
initial-consonant queries such as `ㅇㅂㄷㅇ` to canonical symbols. Ambiguous
short inputs are not forced into a symbol; the orchestrator falls back to the
request or chart symbol and records the resolver result in `agentTrace`.

`shared/gops_agents/intent_understanding/` owns multi-intent decomposition.
It merges content tasks and UI tasks into a `QueryUnderstanding` object:

- `analysis`: role agents only.
- `ui_layout`: UIAgent only.
- `hybrid`: role agents and UIAgent in the same request, for queries such as
  `뉴스 패널 키워주고 애플 뉴스 보여줘`.
- `clarify`: reserved for ambiguous cases that should ask the user before
  executing.

The default provider is deterministic and does not call an LLM. Set
`AGENT_INTENT_CLASSIFIER_PROVIDER=pod` or `openai` to enable the optional cheap
classifier branch. That branch is still executed in parallel with entity and
rules work; Kafka is used for request/result/event boundaries, not for
request/reply inside the hot path.

UI tasks support both single-panel targets and multi-panel sets. A generic
request such as `패널 여러개 띄워줘` maps to the default workspace set
`chart`, `newsFeed`, and `aiSummary`; explicit requests such as
`차트 뉴스 온톨로지 패널 띄워줘` preserve the named panel set.
UI parsing uses the Korean text normalization/fuzzy matching path in
`shared/gops_agents/intent_understanding/ui_parser.py`; aliases and operation
terms live in `config/ui-intent-lexicon.json` and can be replaced with
`AGENT_UI_LEXICON_PATH`.

`shared/gops_agents/orchestration/` owns the workflow nodes, cache helpers,
timing, role execution helpers, request normalization, and report tracing.
`shared/gops_agents/orchestrator.py` remains as a compatibility import shim.

## Shared Package Layout

`shared/gops_agents/` keeps only package boundaries at the top level:

```text
contracts/             report, evidence, route, snapshot, runtime dataclasses
query_understanding/   Korean-first entity/theme resolution
intent_understanding/  parallel content/UI intent decomposition and classifier adapters
orchestration/         request normalization, workflow, routing, timing, tracing
runtime/               admission, queues, workers, report store, delivery gateway
retrieval/             graph expansion, retrieval context, snapshots, cross signals
providers/             news, ontology, macro provider adapters and provider caches
roles/                 logical role agents and AgentContext
synthesis/             final answer synthesis
events/                market event detection and notification publishing
orchestrator.py        compatibility entrypoint for AgentOrchestrator imports
```

Provider status in v1:

- News uses ClickHouse/Redis-backed cached news intelligence, with optional Alpaca direct fallback when explicitly enabled.
- Macro intentionally remains an empty provider adapter.
- Ontology uses GraphDB through `GraphDBOntologyProvider` and returns relationship snapshots.
- Graph-aware retrieval uses `RetrievalContext`; when enabled, graph expansion hints are read from Redis first and ClickHouse second, then bounded related symbols can expand news retrieval.
- Cross-signal join is feature-flagged and combines graph/news/market evidence into compact synthesis signals.

Report persistence uses the `ReportStore` interface. Non-local runtime wiring
uses Redis when `AGENT_REPORT_STORE_BACKEND=auto` and `REDIS_URL` is present;
in-memory storage remains local fallback only.

`/api/agents/analyze` can run as queue-backed async ingress when
`AGENT_ASYNC_ANALYSIS_ENABLED=true`. The API stores a queued report, publishes
an `AgentAnalysisRequestEnvelope` to `agents.analysis-requests.v1`, and returns
`202` with a request id. The worker writes the completed report under the same
id.

`shared/gops_agents/runtime/admission.py` applies the request admission policy before
enqueue. It records queue metrics in the accepted report trace, can reject when
configured backpressure thresholds are crossed, and can degrade stream delivery
requests to polling under backlog.

When `AGENT_DEEP_ANALYSIS_ENABLED=true`, a hot worker can publish the hot answer
as `deep_pending` and enqueue the same request id to
`agents.deep-analysis-requests.v1`. The deep worker writes the follow-up result
back as `deep_completed`, so polling/SSE clients keep using the same report id.
The SSE report endpoint subscribes to the Redis report update channel when
available and falls back to shared-store polling.

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

## Safety Boundary

This system produces analysis, layout proposals, and notifications only. It must
not execute orders or connect directly to account-control flows.
