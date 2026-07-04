# OpenAI Latency Lab

This is a removable experiment folder for measuring OpenAI and GOPS agent latency.
It does not change the frontend, backend API, Kafka contracts, database schema, or
agent runtime code.

## What It Measures

- `openai`: direct OpenAI Responses API latency for small structured-output
  scenarios.
- `pipeline-direct`: `AgentOrchestrator().analyze()` in the same process, with
  OpenAI request timing captured by a temporary `urllib.request.urlopen` wrapper.
- `pipeline-kafka`: Docker/Kafka request path through `agents.analysis-requests.v1`,
  `agent-analysis-worker`, Redis report store, and report polling.

`pipeline-kafka` cannot capture per-call OpenAI timings because the worker runs in
a separate process. Use report timing fields there: `queueWaitMs`, `totalMs`,
`queryUnderstandingMs`, `intentClassifierMs`, `finalAnswerMs`, and `latencyTrace`.

## Environment

Put real credentials only in local `.env` or the shell environment:

```sh
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.2
```

Useful options:

```sh
OPENAI_LATENCY_LAB_MODE=all
OPENAI_LATENCY_LAB_MODELS=gpt-5.2
OPENAI_LATENCY_LAB_REPEAT=5
OPENAI_LATENCY_LAB_WARMUP=1
OPENAI_LATENCY_LAB_STREAM=false
OPENAI_LATENCY_LAB_OUTPUT_PATH=/tmp/openai-latency-lab.json
OPENAI_LATENCY_LAB_CAPTURE_RUNTIME_LOGS=false
```

Modes can be `openai`, `pipeline-direct`, `pipeline-kafka`, or comma-separated.

## Docker From Scratch

If there are no existing containers, build and start the required local runtime:

```sh
docker compose build agent-orchestrator agent-analysis-worker
docker compose up -d kafka redis clickhouse kafka-init agent-orchestrator agent-analysis-worker
```

For `pipeline-kafka`, worker-side OpenAI/provider settings must be present in
`.env` before `agent-analysis-worker` starts. Scenario `env` values are applied
inside the lab process for direct mode, but they cannot reconfigure an already
running worker process.

Run direct OpenAI and same-process pipeline checks:

```sh
docker compose exec agent-orchestrator \
  python -u /app/systems/agent-orchestration/jobs/openai-latency-lab/main.py --mode openai,pipeline-direct
```

Run Kafka end-to-end checks:

```sh
docker compose exec agent-orchestrator \
  python -u /app/systems/agent-orchestration/jobs/openai-latency-lab/main.py --mode pipeline-kafka
```

One-shot container style also works after the image is built:

```sh
docker compose run --rm agent-orchestrator \
  python -u /app/systems/agent-orchestration/jobs/openai-latency-lab/main.py --mode openai
```

## Local Run

Use the repository `.venv` and set `PYTHONPATH` if running outside Docker:

```sh
PYTHONPATH=systems/agent-orchestration/shared:systems/market-data/shared:systems/api-server/pods/api-server/gops-backend \
  .venv/bin/python systems/agent-orchestration/jobs/openai-latency-lab/main.py --mode openai
```

`pipeline-kafka` expects `KAFKA_BOOTSTRAP_SERVERS`, `REDIS_URL`, and a running
agent worker. It is intended for Docker compose.

## Output

The job prints one compact JSON object to stdout. Secrets are never printed.
Rows contain per-iteration timings, and `summary` contains p50/p95 grouped by
mode, scenario, and model.

The direct pipeline mode suppresses runtime `print()` noise so stdout remains
machine-readable JSON. Set `OPENAI_LATENCY_LAB_CAPTURE_RUNTIME_LOGS=true` to copy
the last runtime log lines into each row.

To remove the experiment before merging, delete this folder:

```sh
rm -rf systems/agent-orchestration/jobs/openai-latency-lab
```
