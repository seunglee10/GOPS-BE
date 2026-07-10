# Chart Analysis Assets — Implementation Notes

This file records minimal adjustments made where the approved plan and current
repository contracts differ.

## Bundle 1 — Asset contract and analytics kernel

- Added `shared/chart-contract/chart-analysis-asset.schema.json` because the
  plan defines the asset as the canonical shared contract but does not assign a
  concrete schema path. It lives beside the canonical chart-command schema.
- A `kind="range"` trend carries `rangeFrom`, `rangeTo`, `rangeHigh`, and
  `rangeLow` in addition to the documented common trend fields. The current
  chart-engine `rangeBox` requires two complete time/price anchors, while a
  range intentionally has no pivot anchor IDs.
- Golden fixture JSON stores deterministic scenario recipes rather than long
  duplicated candle arrays. Tests expand those recipes only in the test
  process; production/local runtime never creates synthetic candles.
- Corrected the chart contract checker's existing tick TTL literal to match the
  canonical DDL (`TTL toDateTime(event_time) ...`). This changes no schema or
  retention behavior; it makes the documented parity command validate the DDL
  form already present in both copies.
