# Agent Orchestration Runtime Contract

This document describes the current role-agent runtime contract.

## Scope

`systems/agent-orchestration` produces analysis reports, layout proposals, and
notification decisions. It must not execute orders, control accounts, or turn
analysis into automatic buy/sell actions.

## Runtime Units

| Runtime | Type | Role |
| --- | --- | --- |
| `agent-orchestrator` | pod | Runs logical role agents and returns analysis reports. |
| `agent-event-detector` | pod | Reads market Kafka topics and emits unusual market events. |
| `agent-notification-publisher` | pod | Publishes notification decisions to Redis/WebSocket consumers. |

The logical role agents run inside `agent-orchestrator`:

- `ChartAgent`
- `NewsAgent`
- `MacroAgent`
- `OntologyAgent`
- `UnusualEventExplainerAgent`
- `MarketSummaryAgent`
- `VerificationGuardrailAgent`
- `NotificationDecisionAgent`
- `LayoutAgent`

Only chart, news, macro, and ontology are user-facing roles. Orchestration,
verification, notification, and layout agents are internal steps.

## API Boundary

The API server delegates agent work to `agent-orchestrator`.

```text
POST /api/agents/analyze
GET  /api/agents/reports/{analysis_id}
WS   /ws/agent-alerts
```

The orchestrator service exposes:

```text
GET  /health
POST /analyze
GET  /reports/{analysis_id}
```

## Data Sources

- Chart evidence comes from the chart context supplied by the API/frontend.
- News evidence comes from ClickHouse `news_articles`, loaded from
  `market.news.alpaca.v1`.
- Macro is a staged adapter boundary.
- Ontology evidence comes from GraphDB when `GRAPHDB_SPARQL_URL` is reachable.
- OpenAI is optional and is used only for routing assistance, role analysis, or
  final-answer synthesis. Deterministic fallback must preserve the API shape.

## Kafka Topics

```text
agents.market-events.v1
agents.analysis-requests.v1
agents.analysis-results.v1
agents.notification-decisions.v1
agents.dlq.v1
```

`agent-event-detector` consumes:

```text
market.ticks.v1
market.candles.live.1m.v1
market.candles.closed.v1
```

## Deployment Assets

```text
infra/docker/Dockerfile.gops-agent-orchestrator
docker-compose.yml
infra/k8s/base/deployment-agent-orchestrator.yaml
infra/k8s/base/deployment-agent-event-detector.yaml
infra/k8s/base/deployment-agent-notification-publisher.yaml
infra/k8s/base/service-agent-orchestrator.yaml
infra/k8s/base/statefulset-graphdb.yaml
infra/k8s/base/service-graphdb.yaml
```

## Verification

Run the relevant checks after changing the agent runtime or deployment assets:

```sh
PYTHONPATH=systems/agent-orchestration/shared:systems/market-data/shared python -m unittest discover systems/agent-orchestration/tests -v
PYTHONPATH=systems/market-data/shared:systems/order/shared:systems/order:systems/api-server/pods/api-server/gops-backend python -m unittest discover systems/api-server/tests -v
docker compose config --quiet
kubectl kustomize infra/k8s/base >/tmp/gops-k8s-base.yaml
kubectl kustomize infra/k8s/overlays/aws >/tmp/gops-k8s-aws.yaml
git diff --check
```
