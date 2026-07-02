# Agent Report Storage

This note records the storage direction for agent analysis reports. It does not
activate runtime pods or add database schema.

## Redis Latest Report Store

Purpose:

- short-lived latest report lookup
- recent conversation/report recovery
- cross-process `GET /api/agents/reports/{analysis_id}` support after runtime wiring is enabled

Default TTL:

```text
AGENT_REPORT_TTL_SECONDS=43200
```

Keys:

```text
agent:report:{analysisId}
agent:report:latest:{SYMBOL}
agent:report:latest
```

The value is the serialized `AnalysisReport` JSON shape returned by the agent
orchestrator. Redis failures must fail open so analysis generation is not
blocked by latest-report persistence.

## Postgres Later

Postgres is reserved for long-term report history and audit. Do not add schema
or migrations until the storage boundary is explicitly scheduled.

Future schema should preserve:

- `analysis_id`
- `symbol`
- `intent`
- `created_at`
- status and timing fields
- full report JSON for replay/debug

## Runtime Boundary

The shared code may provide `ReportStore`, `InMemoryReportStore`, and
`RedisReportStore`. Pod entrypoint wiring, env activation, compose, k8s, and
Docker changes are separate runtime work.
