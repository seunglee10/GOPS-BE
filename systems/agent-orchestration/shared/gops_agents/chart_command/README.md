# Chart Command Agent

This directory is the implementation home for the chart-command agent
capability.

The chart-command agent converts a user prompt plus chart runtime context into
frontend chart actions, such as drawing previews, viewport changes, layer
toggles, symbol changes, and other chart-only commands.

## Why This Directory Exists

GOPS currently has two chart-related agent paths:

- The main agent-orchestration workflow owns analysis reports through
  `AgentOrchestrator`.
- The current chart-command/operator path lives in the API server legacy LLM
  route and is used by the central chart input.

The chart-command/operator capability will continue to be developed
independently for now. It should still become a long-term asset of the
agent-orchestration system, not permanent API-server-owned legacy code.

This directory marks the ownership boundary while the implementation is being
extracted from the API server. New chart-command work should start here, even
while compatibility callers still reach the old API route.

## Current Integration Status

This module is intentionally isolated from the main `AgentOrchestrator`
workflow for now.

The chart-command implementation now lives in this package. Compatibility
callers still depend on it through:

- `systems/api-server/pods/api-server/gops-backend/app/routes/llm.py`
- `systems/api-server/pods/api-server/gops-backend/app/services/ai_agents.py`
- `systems/api-server/pods/api-server/gops-backend/app/contracts/chart.py`
- `apps/gops-frontend/src/agent/chartAgent.ts`
- `apps/gops-frontend/src/components/ChartPanel.tsx`

Those locations must be treated as temporary compatibility surfaces. The API
server should keep importing this package instead of owning prompts, schemas, or
OpenAI chart-command calls directly.

During chart-command development, callers may use a development-only toggle to
route chart command requests separately from the production agent workflow. The
toggle exists only to protect the main agent flow while the chart-command agent
is being iterated.

Suggested development toggle name:

```text
CHART_COMMAND_AGENT_DEV_TARGET=legacy
CHART_COMMAND_AGENT_DEV_TARGET=orchestration
```

The exact name may change during implementation, but it must remain clearly
development-scoped. Avoid introducing a permanent production split between the
central agent input and the main agent root.

## Intended Future Shape

The eventual implementation should separate two responsibilities:

- `ChartAnalysisAgent`: contributes chart findings and evidence to analysis
  reports.
- `ChartCommandAgent`: produces validated chart actions for chart operation and
  drawing workflows.

Both capabilities should eventually be owned by the agent-orchestration system.
The public frontend path should converge on the main agent entry point rather
than keeping a separate chart-command API forever.

The staged implementation shape is:

```text
ChartCommandAgent
  -> request/response contracts
  -> chart context normalization
  -> command schema and validation
  -> model/provider boundary
  -> frontend-compatible chart actions or chart commands
```

Do not add FastAPI route dependencies to this package. Keep it usable by the
legacy API wrapper, tests, jobs, and the future `AgentOrchestrator` integration.

## Future Integration Plan

When the main agent workflow is ready to own chart command generation:

1. Keep the extracted chart-command prompt, schema, context normalization, and
   OpenAI boundary in this package.
2. Keep the existing API-server chart LLM route as a thin compatibility wrapper,
   or remove it after callers migrate.
3. Continue developing `ChartCommandAgent` here while the wrapper keeps
   `/api/llm/chat` compatible for dev testing.
4. Route the central frontend agent input through the main agent entry point.
5. Let `AgentOrchestrator` decide when to run chart-command behavior versus
   analysis/report behavior.
6. Keep the chart command output schema compatible with frontend chart action
   validation.

## Cleanup Required At Integration

The development-only separation must not become permanent architecture.

When `ChartCommandAgent` is integrated into `AgentOrchestrator`, remove:

- Development-only chart-command routing toggles.
- Temporary frontend routing branches that bypass the main agent entry point.
- API-server prompt/schema duplication for chart command generation.
- Compatibility wrappers that only exist for the old `/api/llm/chat` path,
  unless a short deprecation window is explicitly required.
- Migration-only README notes in dependent folders once they no longer describe
  live code.

## Non-Goals For This Directory

- Do not move the main `AgentOrchestrator` workflow here.
- Do not add Kubernetes pods per chart sub-agent.
- Do not make chart command development mutate the production report workflow
  until the integration point is intentionally designed.
- Do not keep a permanent public API split between chart commands and the main
  agent input.
