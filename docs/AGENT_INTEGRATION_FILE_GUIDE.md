# Agent Integration File Guide

Owner: agent handoff
Date: 2026-07-03

Use this when building a new frontend, backend, or AWS deployment around the
current agent runtime. It lists the existing files that define the agent
contract. The new teams do not need to keep the current frontend/backend/AWS
implementation, but they must preserve the contracts these files document or
implement.

## Read Order

1. `docs/AGENT_ARCHITECTURE.md`
2. `systems/agent-orchestration/README.md`
3. This file
4. `docs/ENVIRONMENT.md`
5. `docs/IMAGE_STRATEGY.md` only if image boundaries change
6. `docs/ARCHITECTURE.md` only if runtime topology changes

## Agent Core

These are the files to keep if the current agent runtime is reused.

| Area | Files | Why they matter |
| --- | --- | --- |
| Runtime package | `systems/agent-orchestration/shared/gops_agents/**` | Agent contracts, orchestration, query understanding, providers, queues, report store, synthesis, events. |
| Entrypoints | `systems/agent-orchestration/pods/**/main.py` | Commands used by containers for orchestrator, workers, delivery, classifier, events, notifications. |
| Jobs | `systems/agent-orchestration/jobs/**/main.py` | Smoke, graph refresh, latency, retrieval, fanout, and grounding checks. |
| Config | `systems/agent-orchestration/config/*` | UI intent lexicon and fallback entity aliases. |
| Tests | `systems/agent-orchestration/tests/test_agent_orchestration.py` | Best single guard for agent contract regressions. |
| README | `systems/agent-orchestration/README.md` | Code-local runtime and validation notes. |

Current provider dependency:

```text
systems/market-data/shared
```

The agent imports `alfaka.*` helpers from market-data shared code. If the new
backend/AWS design does not want that dependency, replace it deliberately with
agent-owned provider interfaces rather than copying isolated helper functions.

## Frontend Integration

The new frontend can be built from scratch. Use these files only to understand
request shape, report shape, chart/layout context, and UI commands.

| Need | Existing reference files | Preserve |
| --- | --- | --- |
| Agent request/report shape | `apps/gops-frontend/src/agents/agentAnalysis.ts` | Request fields, report normalization, final answer/report display expectations. |
| Chat submit flow | `apps/gops-frontend/src/components/SystemArea.tsx` | `POST /api/agents/analyze` usage, message context, layout context handoff. |
| Chart-context reference | `apps/chart-engine/src/agentReference.ts` | How a chart panel/document is referenced from an agent request. |
| Chart proposal response | `apps/chart-engine/src/agentChat.ts` | How agent chart commands become a chart proposal. |
| Chart proposal commands | `apps/chart-engine/src/proposals.ts`, `apps/chart-engine/src/types.ts` | Chart command payload shape if chart manipulation is kept. |
| Layout proposal commands | `apps/gops-frontend/src/layout/types.ts`, `apps/gops-frontend/src/layout/commands.ts`, `apps/gops-frontend/src/layout/panelRegistry.ts` | Panel/layout proposal shape if UI layout control is kept. |

Frontend must call or support:

```text
POST /api/agents/analyze
GET  /api/agents/reports/{analysis_id}
GET  /api/agents/reports/{analysis_id}/stream
WS   /ws/agent-alerts
```

If the new frontend does not support chart/layout agent actions, keep the text
analysis flow and ignore `layoutProposal` or `chartProposal` fields explicitly.

## Backend Integration

These files define the backend bridge. A new backend can replace the
implementation, but it should keep the same ingress, report, idempotency, and
streaming semantics.

| Need | Existing reference files | Preserve |
| --- | --- | --- |
| Request schema | `systems/api-server/pods/api-server/gops-backend/app/contracts/agents.py` | Permissive request body that forwards unknown agent context safely. |
| HTTP routes | `systems/api-server/pods/api-server/gops-backend/app/routes/agents.py` | Analyze, report polling, SSE stream, alert WebSocket route behavior. |
| Queue/report bridge | `systems/api-server/pods/api-server/gops-backend/app/services/agent_gateway.py` | Async flag, idempotency, admission, report store, queue submit, compatibility HTTP path. |
| Alert parsing | `systems/api-server/pods/api-server/gops-backend/app/services/agent_alert_payloads.py` | Redis pubsub payload parsing for alert WebSocket. |
| Router wiring | `systems/api-server/pods/api-server/gops-backend/app/main.py` | Agent route registration and import path expectations. |
| Contract tests | `systems/api-server/tests/test_agent_routes.py` | Best backend-side guard for route and gateway behavior. |

Backend rules:

- Do not call `AgentOrchestrator.analyze()` directly from request handlers.
- Async mode should enqueue an `AgentAnalysisRequestEnvelope` and return `202`.
- Idempotent retries should reuse the same request id when possible.
- Report polling and SSE must read from the shared report store or degrade
  predictably.
- Compatibility mode may call `agent-orchestrator` over HTTP when
  `AGENT_ASYNC_ANALYSIS_ENABLED=false`.

## AWS And Platform Integration

These files define the deployable shape of the current agent runtime.

| Need | Existing reference files | Preserve |
| --- | --- | --- |
| Agent image | `infra/docker/Dockerfile.gops-agent-orchestrator` | Python path, copied source folders, runtime dependency assumptions. |
| Required deployments | `infra/k8s/base/deployment-agent-orchestrator.yaml`, `infra/k8s/base/deployment-agent-analysis-worker.yaml`, `infra/k8s/base/deployment-agent-delivery-gateway.yaml` | Orchestrator, hot worker, result delivery. |
| Optional deployments | `infra/k8s/base/deployment-agent-intent-classifier.yaml`, `infra/k8s/base/deployment-deep-analysis-worker.yaml`, `infra/k8s/base/deployment-agent-event-detector.yaml`, `infra/k8s/base/deployment-agent-notification-publisher.yaml` | Classifier, deep analysis, event detection, notification fanout. |
| Services | `infra/k8s/base/service-agent-orchestrator.yaml`, `infra/k8s/base/service-agent-intent-classifier.yaml`, `infra/k8s/base/service-graphdb.yaml` | Internal service names and ports. |
| Jobs | `infra/k8s/base/job-agent-queue-smoke.yaml`, `infra/k8s/base/job-report-store-smoke.yaml`, `infra/k8s/base/job-graph-expansion-*.yaml`, `infra/k8s/base/job-retrieval-*.yaml`, `infra/k8s/base/job-fanout-policy-benchmark.yaml`, `infra/k8s/base/job-answer-grounding-eval.yaml` | Smoke, graph refresh, retrieval, latency, fanout, and grounding checks. |
| Config | `infra/k8s/base/configmap.yaml`, `infra/k8s/overlays/aws/configmap-aws-patch.yaml`, `infra/k8s/overlays/aws-incluster-app/configmap-incluster-patch.yaml` | `AGENT_*`, Kafka, Redis, ClickHouse, GraphDB, OpenAI model env. |
| Kustomize wiring | `infra/k8s/base/kustomization.yaml`, `infra/k8s/overlays/aws/kustomization.yaml`, `infra/k8s/overlays/aws-incluster-app/kustomization.yaml` | Which agent resources are included and which image is deployed. |
| OpenAI secret | `infra/k8s/overlays/aws/externalsecret-openai.yaml`, `infra/k8s/overlays/aws-incluster-app/externalsecret-openai.yaml`, `platform/secrets/README.md` | Secret name and expected `OPENAI_API_KEY` property. |
| Kafka topics | `platform/kafka/topics.txt`, `platform/kafka/README.md` | Agent topic names. |
| ClickHouse schema | `infra/clickhouse/initdb/01-market-data.sql` | `market_data.agent_graph_expansions` and news/symbol tables used by providers. |
| Terraform reference | `infra/aws/terraform/README.md`, `infra/aws/terraform/variables.tf`, `infra/aws/terraform/terraform.tfvars.example` | ECR/secret naming references if AWS foundation is reused. |

Required AWS path:

```text
Build gops-agent-orchestrator
-> create Kafka topics
-> provision Redis/Valkey
-> provision ClickHouse schema
-> inject OPENAI_API_KEY and CLICKHOUSE_PASSWORD
-> deploy agent-orchestrator
-> deploy agent-analysis-worker
-> deploy agent-delivery-gateway
-> enable AGENT_SHARED_REPORT_STORE_ENABLED=true
-> enable AGENT_ASYNC_ANALYSIS_ENABLED=true
```

## Env And Secrets

Files to check when env names or defaults change:

```text
.env.example
systems/api-server/.env.example
docs/ENVIRONMENT.md
infra/k8s/base/configmap.yaml
infra/k8s/overlays/aws/configmap-aws-patch.yaml
infra/k8s/overlays/aws-incluster-app/configmap-incluster-patch.yaml
platform/secrets/README.md
```

High-impact env groups:

```text
AGENT_ASYNC_ANALYSIS_ENABLED
AGENT_SHARED_REPORT_STORE_ENABLED
AGENT_ANALYSIS_QUEUE_BACKEND
AGENT_REPORT_STORE_BACKEND
AGENT_REPORT_STREAM_REDIS_ENABLED
AGENT_ANALYSIS_REQUESTS_TOPIC
AGENT_DEEP_ANALYSIS_REQUESTS_TOPIC
AGENT_ANALYSIS_RESULTS_TOPIC
AGENT_QUERY_UNDERSTANDING_EVENTS_TOPIC
AGENT_NOTIFICATION_DECISIONS_TOPIC
AGENT_MARKET_EVENTS_TOPIC
AGENT_DLQ_TOPIC
KAFKA_BOOTSTRAP_SERVERS
REDIS_URL
CLICKHOUSE_HTTP_URL
GRAPHDB_SPARQL_URL
OPENAI_API_KEY
```

Do not commit real `.env`, API keys, token caches, access-key CSV files, or
copied secret payloads.

## Merge Hotspots

Watch these files first during merge conflict resolution:

```text
docs/AGENT_ARCHITECTURE.md
docs/AGENT_INTEGRATION_FILE_GUIDE.md
systems/agent-orchestration/README.md
systems/agent-orchestration/shared/gops_agents/contracts/__init__.py
systems/agent-orchestration/shared/gops_agents/runtime/envelope.py
systems/agent-orchestration/shared/gops_agents/runtime/queues.py
systems/agent-orchestration/shared/gops_agents/runtime/report_store.py
systems/agent-orchestration/shared/gops_agents/runtime/workers.py
systems/agent-orchestration/shared/gops_agents/retrieval/snapshots.py
systems/api-server/pods/api-server/gops-backend/app/routes/agents.py
systems/api-server/pods/api-server/gops-backend/app/services/agent_gateway.py
infra/docker/Dockerfile.gops-agent-orchestrator
infra/k8s/base/configmap.yaml
infra/k8s/base/kustomization.yaml
platform/kafka/topics.txt
infra/clickhouse/initdb/01-market-data.sql
```

Default conflict policy:

- Preserve API route names unless the frontend/backend contract is explicitly
  changed.
- Preserve Kafka topic names unless all workers, backend queue submitters, and
  platform topic creation are changed together.
- Preserve Redis report key semantics unless polling, SSE, and idempotency
  behavior are changed together.
- Preserve `systems/market-data/shared` in the agent image until provider
  interfaces are split out.
- Keep deleted legacy docs deleted; move any still-useful content into this
  file or `docs/AGENT_ARCHITECTURE.md`.

## Validation Checklist

Run after integration or merge:

```sh
find docs -maxdepth 1 -type f -name '*.md' -print | sort
git diff --check
.venv/bin/python -m unittest discover -s systems/agent-orchestration/tests -p 'test_agent_orchestration.py'
.venv/bin/python -m unittest discover -s systems/api-server/tests -p 'test_agent_routes.py'
```

Runtime acceptance:

```text
POST /api/agents/analyze returns 202 and analysisId
agent-analysis-worker consumes agents.analysis-requests.v1
completed report is saved in Redis
GET /api/agents/reports/{analysis_id} returns the completed report
GET /api/agents/reports/{analysis_id}/stream emits updates or falls back to polling
```
