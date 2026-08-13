# API Contracts Notes

This folder owns request and response models that are specific to the API
server boundary.

## Chart Command Migration

`chart.py` currently includes legacy chart-command request models used by
`/api/llm/chat` and re-exports chart-command schema helpers from the shared
agent package.

Chart-command contracts that describe the agent capability itself belong in:

```text
systems/agent-orchestration/shared/gops_agents/chart_command/
```

Keep only API boundary adapters here while the legacy route exists. Avoid adding
new chart-command capability schemas in this folder.

When the frontend and backend converge on the main agent entry point, remove the
old `/api/llm/chat` contract models that are no longer used and delete this
migration note.
