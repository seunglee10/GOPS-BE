# GOPS Agent Instructions

## Before Work

- Read the current code and `DesignConcept.md` before changing behavior.
- Treat `docs/spec/` as reference specs from the wider team, not as hard constraints.
- If an implementation direction differs from `docs/spec/`, do not block automatically. Report the difference, why it matters, and the reason for the chosen direction.
- Do not reference `ref/`; the old reference folder has been removed.

## DesignConcept Log

- Every implementation change must append a short log entry to `DesignConcept.md`.
- Use this format:
  - `### YYYY-MM-DD: Title`
  - `- 변경:`
  - `- 판단:`
  - `- 유지할 계약:`
  - `- 검증:`
- Keep entries concise, factual, and useful for future Cho Hyunho / Kim Heejun merge decisions.

## Project Rules

- Preserve the current `apps/`, `packages/`, `services/`, and `infra/` structure unless the user asks for restructuring.
- Preserve the market-data chart contracts:
  - REST `/api/charts/candles` owns historical snapshot and range loading.
  - WebSocket owns live updates, reconnect gap-fill/control, and live delta behavior.
  - Chart API reads Redis and ClickHouse for serving data.
  - ClickHouse `chart_candles` is the serving projection.
  - S3 is durable storage and replay/rematerialization basis.
- Do not stage credentials, local artifacts, generated outputs, `.env`, `.venv`, `node_modules`, or build output.
- Do not push unless the user explicitly asks.
