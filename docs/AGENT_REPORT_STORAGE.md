# Agent Report Storage

This note records the storage direction for agent analysis reports. Redis
short-lived report storage is wired for async agent runtime; it does not add a
Postgres long-term history schema.

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
agent:request:idempotency:{userHash}:{keyHash}
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

The shared code provides `ReportStore`, `InMemoryReportStore`, and
`RedisReportStore`. Non-local runtime uses Redis when
`AGENT_REPORT_STORE_BACKEND=auto` and `REDIS_URL` is present. The
`agent-analysis-worker` writes completed reports under the request id used by
the API admission path.
