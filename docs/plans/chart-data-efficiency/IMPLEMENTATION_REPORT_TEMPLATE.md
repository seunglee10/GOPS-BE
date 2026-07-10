# Chart Data Efficiency Goal Report

## Goal

- Workstream:
- Commit(s):
- Started/completed:
- Status: complete / rolled back / blocked

## Scope delivered

- Planned items completed:
- Explicitly deferred items:
- Files/owners touched:

## Decisions made during implementation

| Decision | Options considered | Choice and reason | Contract/core marker |
| --- | --- | --- | --- |
| | | | none / `[CONTRACT-CHANGE]` / `[CORE-TUNING]` |

## Differences from plan

| Planned | Implemented | Reason | Follow-up required |
| --- | --- | --- | --- |
| | | | |

Any `[CONTRACT-CHANGE]` not listed in the approved plan stops implementation until user approval.
Any discovered axis/zoom/price-scale change requires a `[CORE-TUNING]` addendum and separate approval.

## Contract migration status

- Contract ID(s):
- Reader-first release complete:
- Dual-read/write state:
- Compatibility counter and result:
- Rollback switch tested:
- Operator action still required:

## Code-level measurements

| Metric/scenario | Before | After | Fixture/spy test |
| --- | ---: | ---: | --- |
| Redis commands | | | |
| Kafka sends | | | |
| state entries after N events | | | |
| S3 objects/PUTs | | | |
| provider reads/calculations | | | |
| browser cache entries | | | |

No production baseline is expected.

## Visual equivalence

- Desktop snapshots:
- Mobile snapshots:
- Candle / line / ohlc / bidask:
- SMA / optional indicators / candle VP / compare / order-flow panel:
- Pixel-diff result and approved threshold:
- Overlap checklist result:

## Validation

| Command | Result | Notes |
| --- | --- | --- |
| compileall | | |
| market-data unittest | | |
| api-server unittest | | |
| frontend tsc | | |
| frontend build | | |
| chart runtime tests | | |
| chart visual tests | | |
| compose config | | |
| compose build | | |
| `git diff --check` | | |

## Operator after-deploy slot

The agent does not run these commands against production.

- Observation window/date:
- Active symbol/session count:
- Redis `INFO commandstats` delta:
- Kafka messages/bytes/lag:
- ClickHouse rows/bytes/parts by table/day:
- S3 PUT/LIST/GET, bytes, objects by prefix:
- Compatibility consumer/fallback counters:
- Anomalies and rollback decision:

## Rollback readiness

- Switch/commit to revert:
- Data written during Goal remains readable:
- Irreversible expiry/deletion performed: must be `none` for agent-run work
- Final rollback drill result:
