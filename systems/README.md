# GOPS Systems

`systems/` contains feature-owned backend domains.
Each system owns its runtime entrypoints, shared code, tests, and README.

```text
api-server/    FastAPI chart/order/WebSocket gateway
market-data/   Alpaca ingest, stream processing, storage, backfill
order/         KIS demo order domain, outbox, adapter, jobs
agent-orchestration/ Role-based agents, event detection, notifications
```

Before adding backend code, choose the owning system.
If no current system fits, read `../AGENTS.md` and the relevant agent handoff
doc under `../docs/` before creating a new one.
