# Repository Agent Instructions

## Design Concept Maintenance

When modifying code in this repository, always review and update `DesignConcept.md`
before finishing the task.

The update should explain:

- what was changed
- why it was changed
- how the implementation was shaped
- which contracts or assumptions must be preserved
- what future merge risks or choices were introduced

This repository is expected to be merged later with teammate branches using Codex. The
future merge process will compare this branch's `DesignConcept.md` with the teammate's
`DesignConcept.md`, so the document must capture design judgment, not only a mechanical
diff summary.

Keep the update concise but specific enough for a future merge assistant to understand
which behavior should win when branches disagree.

## Merge-Sensitive Project Rules

- Preserve the current `apps/`, `packages/`, `services/`, and `infra/` structure unless
  the user explicitly asks for a restructuring.
- Prefer behavior-level integration over wholesale replacement when teammate branches use
  incompatible directory layouts.
- Preserve the market-data chart contracts:
  - REST `/api/charts/candles` owns historical snapshot loading.
  - WebSocket owns live updates, reconnect gap-fill/control, and live delta behavior.
  - Chart API reads Redis and ClickHouse, not S3 directly.
  - ClickHouse `chart_candles` is the serving projection.
  - S3 is durable storage and replay/rematerialization basis.
- Do not commit or push unless the user explicitly asks.
- Do not stage credentials, local artifacts, or planning-only files unless the user
  explicitly asks for them.
