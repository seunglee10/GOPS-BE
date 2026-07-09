# Codex Goal-Mode Prompt (copy-paste below the line)

---

## Goal

Implement the **Bid-Ask Volume Profile (Order Flow Profile)** feature in this repository, end to
end, exactly as specified by the plan set in `bid_ask_vp_plan/`. This includes fully removing the
legacy `footprint` feature it replaces, while preserving every other existing feature (candles,
indicators, candle-based volume-profile overlay, compare, orders, agent flows).

## Read first, in this order

1. `bid_ask_vp_plan/00_OVERVIEW.md` — goal, fixed decisions, target architecture, canonical names,
   document precedence. Then `01` → `02` → `03` → `04` → `05` → `06` in the same folder.
2. `AGENTS.md`, `docs/CHART_DATA_REBUILD_PLAN.md`, `docs/STRUCTURE_GUIDE.md`.

Precedence: **`bid_ask_vp_plan/` is the source of truth for this task.** It supersedes the
original design handoff (already removed from the repo); mentions of "the handoff" in the plan are
historical rationale. The user has explicitly approved the API/schema changes listed in
`00_OVERVIEW.md` ("Document precedence & AGENTS.md compliance"); no AGENTS.md-protected route is
touched.

## How to work

- Follow the staged order in `bid_ask_vp_plan/06_rollout_validation_and_tests.md` §1 (stages 1–8).
  Footprint removal (stage 7) must come only after the new order-flow paths compile and their
  tests pass.
- After each stage, run the applicable subset of the validation gate in `06` §6; at the end, the
  full gate must be green. Use the repo-root `.venv` (Python 3.12) and the PYTHONPATH shown in
  `README.md` / `06` §6 for Python checks.
- The plan specifies contracts (schema DDL, Redis keys, REST/WS payload shapes, event types,
  component boundaries, naming) — treat these as fixed. Mechanical details (a helper renamed, a
  line moved, an import path) may differ from the plan's snapshots: verify against current code
  and adapt without changing the contracts.
- Where the plan explicitly delegates a choice ("implementer's choice", "verify and adapt"), use
  your judgment and note it. If you find a contract-level problem that forces a deviation
  (something that cannot work as specified), implement the closest working alternative and record
  it in a new file `bid_ask_vp_plan/DEVIATIONS.md` with the reason — do not silently drift.
- Write the tests specified in each plan file alongside the code (inventory in `06` §2). Do not
  weaken or delete unrelated existing tests; update only those listed in
  `05_footprint_removal.md`.
- Do not generate fake market candles, do not touch the order/KIS domains, do not commit secrets,
  do not push (repo rules in `AGENTS.md`).
- Update the project docs listed in `05` §3 and `06` §1 stage 8 in the same change.

## Definition of done

All eight success criteria in `bid_ask_vp_plan/06_rollout_validation_and_tests.md` §8, including:
the full validation gate in `06` §6 passes; `grep -ri footprint` matches only the documentation
noted in `05` §5; the three `GET /api/charts/order-flow/*` endpoints, the
`ORDER_FLOW_BINS_UPDATE` WS event, the `order_flow_profile_daily` DDL (both initdb copies), the
EOD rollup job + CronJob manifest, the `bidask` chart type, and the `orderFlow` tile panel all
exist and behave per the plan. Summarize what you built, test results, any `DEVIATIONS.md`
entries, and the operator runbook pointer (`06` §7) in your final report.
