# GOPS Agent Instructions

These rules are for Codex and future contributors.

## Read First

- `docs/PRODUCT_CONTEXT.md` for product direction.
- `docs/STRUCTURE_GUIDE.md` before adding a feature, pod, job, image, platform dependency, or new folder.
- `docs/ARCHITECTURE.md`, `docs/IMAGE_STRATEGY.md`, and `docs/ENVIRONMENT.md` before changing runtime, Docker, compose, k8s, env, or AWS assets.
- Current code before changing paths or imports.
Use current code, this file, and the docs in `docs/` as the source of truth. If an older conversation or copied prompt conflicts with them, report the conflict before reshaping the project.

## Structure Rules

- Keep feature code under `systems/<system>`.
- Keep UI code under `apps/`.
- Keep external/runtime dependency contracts under `platform/`.
- Keep Docker, compose, k8s, Terraform, and AWS deployment assets under `infra/`.
- Use root `shared/` only for stable cross-system contracts.
- Preserve Python namespaces:
  - `alfaka.*` from `systems/market-data/shared`.
  - `kis_trader.*` from `systems/order/shared`.

## Behavior Rules

- Do not change API behavior, order behavior, chart behavior, KIS adapter behavior, Kafka message contracts, or DB schema during structure-only work.
- Import/path edits are allowed only when required by file movement.
- Do not generate fake market candles in local runtime.
- Do not push unless the user asks.
- Use the repository-root `.venv` as the only official local Python virtualenv.
- Do not create duplicate project virtualenvs under `/tmp` or other ad hoc paths.
- Use Python 3.12 for local Python checks, matching the Docker images.

## API Rules

Preserve these routes unless the user explicitly changes the API contract:

```text
GET  /api/charts/candles
POST /api/charts/backfill
GET  /api/charts/backfill/status
GET  /api/charts/symbols
WS   /ws/charts
GET  /api/order-contract
POST /api/orders
GET  /api/orders/{order_id}
GET  /api/orders/{order_id}/events
WS   /ws/orders/{order_id}
```

`POST /api/orders` must require `Idempotency-Key`.
`KIS_ENV=real` remains disabled for v1.

## Secret Rules

Never commit:

```text
.env
access key CSV files
KIS token caches
node_modules/
dist/
local caches
real credentials
```

## Documentation Rules

- Keep durable docs short and current.
- Prefer one clear README at each system or platform boundary.
- Update README, structure, image, env, compose, and k8s docs in the same change when runtime boundaries change.
- Product-context docs guide direction; they are not permission to implement missing features without a task.
