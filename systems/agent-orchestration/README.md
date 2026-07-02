# GOPS Agent Orchestration

`agent-orchestration` owns the v1 role-based stock analysis agent runtime.

## Runtime Units

| Runtime | Type | Role |
| --- | --- | --- |
| `agent-orchestrator` | pod | Compatibility HTTP endpoint for direct analysis and report lookup. |
| `agent-analysis-worker` | pod | Consumes queued analysis requests and writes shared reports. |
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

`shared/gops_agents/admission.py` applies the request admission policy before
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
agents.notification-decisions.v1
agents.dlq.v1
```

## Safety Boundary

This system produces analysis, layout proposals, and notifications only. It must
not execute orders or connect directly to account-control flows.
