# GOPS Agent Orchestration

`agent-orchestration` owns the v1 role-based stock analysis agent runtime.

## Runtime Units

| Runtime | Type | Role |
| --- | --- | --- |
| `agent-orchestrator` | pod | Runs logical role agents and returns analysis reports. |
| `event-detector` | pod | Reads market Kafka topics and emits unusual market events. |
| `notification-publisher` | pod | Publishes notification decisions to Redis/WebSocket consumers. |

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

News, macro, and ontology use empty provider adapters in v1. Real external APIs
are intentionally not selected here.

## Kafka Topics

```text
agents.market-events.v1
agents.analysis-requests.v1
agents.analysis-results.v1
agents.notification-decisions.v1
agents.dlq.v1
```

## Safety Boundary

This system produces analysis, layout proposals, and notifications only. It must
not execute orders or connect directly to account-control flows.
