# API Services Notes

This folder contains API-server service adapters. It should not permanently own
agent runtime logic that belongs to `systems/agent-orchestration`.

## Chart Command Migration

`ai_agents.py` is a compatibility adapter for legacy chart-command behavior
used by `/api/llm/chat`.

The target home for that behavior is:

```text
systems/agent-orchestration/shared/gops_agents/chart_command/
```

During the transition, keep only API-server adapter code here. Prompt
construction, chart context handling, command schema use, OpenAI calls, and
response parsing belong in `ChartCommandAgent`, not in this folder. The desired
end state is that `ai_agents.py` disappears after the old `/api/llm/chat` route
is removed.

When `ChartCommandAgent` is called through `AgentOrchestrator`, remove this
compatibility service path and delete this migration note.
