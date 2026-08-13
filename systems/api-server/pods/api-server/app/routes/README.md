# API Routes Notes

This folder owns FastAPI route wiring only.

## Chart Command Migration

`llm.py` exposes the legacy chart-command routes:

```text
POST /api/llm/chat
POST /api/llm/chart-proposal
```

These routes are temporary compatibility surfaces while `ChartCommandAgent` is
split out and developed under:

```text
systems/agent-orchestration/shared/gops_agents/chart_command/
```

Do not add new chart-command prompt, schema, or provider logic in this folder.
During the migration, `llm.py` must remain a thin wrapper that validates auth,
adapts FastAPI request/response objects, and delegates chart-command behavior to
the shared `ChartCommandAgent` package.

When chart command handling is integrated into `AgentOrchestrator` and the
frontend no longer calls `/api/llm/chat`, remove the compatibility route and
delete this migration note if no other route depends on it.
