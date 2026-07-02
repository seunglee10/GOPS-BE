# GOPS Docs

Reference docs for future implementation.
The root `README.md` stays short; repo-wide Codex rules live in root `AGENTS.md`; deeper project docs live here.

| File | Purpose |
| --- | --- |
| `PRODUCT_CONTEXT.md` | Product direction and current/future scope boundary. |
| `STRUCTURE_GUIDE.md` | Folder placement rules for future code. |
| `ARCHITECTURE.md` | Current system, pod/job, and platform relationships. |
| `CHART_DATA_REBUILD_PLAN.md` | Planned chart/market-data rebuild contract: empty start, on-demand backfill, Redis 120-bar state, SIP/BOATS exclusivity, and monitoring workbench. |
| `IMAGE_STRATEGY.md` | Docker image boundaries and naming rules. |
| `ENVIRONMENT.md` | Env, secret, and platform contracts. |
| `AGENT_ORCHESTRATION_IMPLEMENTATION.md` | Summary of the role-based multi-agent implementation and Docker validation. |
| `../AGENTS.md` | Codex/contributor rules for this repo. |

Old long-form specs were removed to avoid stale, conflicting guidance.
If a future long-form spec is needed, add it under `docs/` with a clear owner and date.
