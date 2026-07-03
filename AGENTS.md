# GOPS Front Agent Instructions

## Working Scope

- Main application code lives in `frontend/`.
- CDC-compatible local demo backend code lives in `mock_backend/`.
- Chart agent backend code lives in `agent_backend/`.
- Durable chart contracts live in `docs/CDC/`.
- `regacy_front/` is a reference archive for previous frontend work.

## CDC Rules

- Treat `docs/CDC/CDC-proposal.md` as the chart data backend request document.
- Update `docs/CDC/CDC-proposal.md` only when frontend/mock behavior changes the future real chart data backend contract.
- Do not put chart-agent API details, chart-agent implementation notes, or OpenAI behavior in CDC.
- Do not add chart data assumptions only in code. If the real chart data backend must provide it, write it into `CDC-proposal.md`.

## Frontend Rules

- Keep the first screen focused on three panels: one chart panel and two planned panels.
- Keep chart rendering code small, readable, and tied to the CDC DTOs.
- Keep chart-agent behavior chart-scoped. The agent may request only chart actions that a user can also perform through frontend controls.
- Apply user actions and chart-agent actions through the same chart action reducer.
- Do not introduce trading, account, deployment, infra, or production backend concerns unless explicitly requested.

## Agent Backend Rules

- Keep `agent_backend/` separate from `mock_backend/`; it is expected to become a separate Kubernetes pod later.
- The chart agent must request needed market/chart data from the chart data backend.
- The frontend must not expose OpenAI keys and must not call OpenAI directly.
- If the chart agent needs a chart data field that the real backend must provide, update `docs/CDC/CDC-proposal.md` in the same change.

## Mock Backend Rules

- The mock backend exists only to emit CDC-shaped test data for frontend development.
- Keep mock data deterministic enough for UI testing.
- Do not add mock-only fields to chart DTOs unless they are also proposed in CDC.
- The mock backend should be replaceable by the real chart backend without frontend contract changes.

## Safety Rules

- Never commit `.env`, credentials, token caches, `node_modules/`, `dist/`, local caches, or real market access keys.
- Do not push unless the user asks.
