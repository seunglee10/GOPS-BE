# Backend Chart Merge Report

## 기준

- Base branch: latest `origin/dev` at `feb33df8`.
- Source commit: local `80648a2` (`Stabilize market data backfill and chart rendering`).
- Merge branch: `codex/backend-chart-merge`.

## 유지한 담당 변경

- Market-data backend serving/backfill/storage contract under `systems/market-data`.
- Chart API backend contract under `systems/api-server`:
  - candles, watchlist, hot-symbols, backfill/status/queue behavior.
  - canonical `v2` + `split` serving guard.
  - S3-first materialize, Alpaca fallback, derived interval source handling.
- ClickHouse market-data schema updates for canonical/session-aware candle rows.
- GOPS20 universe, Hot Top10, Redis Streams backfill, S3 compact manifest/materializer tests.

## 팀 코드 우선 유지

- `apps/gops-frontend/**`
- `apps/chart-engine/**`
- `infra/docker/nginx/gops-frontend.conf`
- agent/order/KIS/auth/panel UI/general frontend runtime

No frontend or chart-engine files were overwritten in this merge branch.

## 수동 포팅한 설정

- `.env.example`, `systems/api-server/.env.example`
- `docker-compose.yml`
- `infra/k8s/base/configmap.yaml`
- `infra/k8s/base/job-initial-load.yaml`
- `infra/k8s/overlays/aws/configmap-aws-patch.yaml`
- `docs/ENVIRONMENT.md`

Only market-data settings were ported: `gops20`, Hot Top10, `HISTORICAL_ADJUSTMENT=split`, canonical guards, S3 materialize knobs, and `BACKFILL_INITIAL_LOAD_1M_MIN_START=2023-07-01T00:00:00Z`.

## 검증 결과

- `pytest systems/api-server/tests/test_market_data_query.py`: 34 passed.
- `PYTHONPATH=systems/market-data/shared pytest systems/market-data/tests/test_market_data_hardening.py`: 150 passed.
- `docker compose config --quiet`: passed.
- `kubectl kustomize infra/k8s/base`: passed.
- `kubectl kustomize infra/k8s/overlays/aws`: passed.

## 남은 운영 확인

- 실제 Alpaca live-market hours ingest smoke.
- S3 processed/compact manifest to ClickHouse materialize smoke against the target AWS bucket.
- Browser smoke after team frontend starts on this merge branch.
