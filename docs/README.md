# GOPS Docs

Reference docs for current implementation and future handoff.
The root `README.md` stays short; repo-wide Codex rules live in root `AGENTS.md`; architecture and project docs live here.

| File | Purpose |
| --- | --- |
| `PRODUCT_CONTEXT.md` | Product direction and current/future scope boundary. |
| `STRUCTURE_GUIDE.md` | Folder placement rules for future code. |
| `ARCHITECTURE.md` | Current system, pod/job, and platform relationships. |
| `IMAGE_STRATEGY.md` | Docker image boundaries and naming rules. |
| `ENVIRONMENT.md` | Env, secret, and platform contracts. |
| `AGENT_ARCHITECTURE.md` | Canonical snapshot-based agent architecture and runtime contracts. |
| `GRAPHDB_RELATIONSHIP_AGENT_HANDOFF.md` | GraphDB Relationship Agent implementation handoff for SPARQL, relation normalization, scoring, and cache work. |
| `../AGENTS.md` | Codex/contributor rules for this repo. |

Stale role-agent and merge-handoff specs were removed to avoid conflicting guidance.
If a future long-form spec is needed, add it under `docs/` with a clear owner and date.
