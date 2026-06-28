# GOPS Chart Interval / Backfill / Aggregation V1

이 문서는 Goal 모드 구현 기준 문서다. 구현 중 결정이 흔들릴 때는 이 파일과 `DesignConcept.md`의 최신 로그를 우선 확인한다.

## Interval Contract

- Canonical interval은 `1m`, `5m`, `10m`, `1D`, `1W`, `1M`이다.
- Legacy 입력 `1d`는 API/UI/command boundary에서 `1D`로 normalize한다.
- API 응답, chart document, chart command, provider 내부 상태는 canonical interval만 사용한다.

## Initial Snapshot Policy

초기 차트 로드는 시간 범위가 아니라 화면을 채우는 bar count 기준이다.

| interval | default visible bars |
|---|---:|
| `1m` | 390 |
| `5m` | 390 |
| `10m` | 390 |
| `1D` | 250 |
| `1W` | 260 |
| `1M` | 120 |

이 값은 chart document seed, timeframe 변경, viewport reset, snapshot fetch, comparison snapshot fetch에 동일하게 적용한다.

`1M`은 기본 화면이 120개지만 5년 backfill 범위에서는 실제 월봉이 약 60개다. 따라서 화면 요청 상한은 `max(default visible bars, backfill target bars)`로 두고, 백필 완료 판단은 아래 target range 기준으로 별도 계산한다.

## Backfill Policy

Backfill은 화면 표시용 bar count와 분리된 저장/수집 목표다.

| group | interval | source | target range |
|---|---|---|---:|
| Intraday | `1m` | Alpaca `1Min` bars | 1 year |
| Intraday derived | `5m`, `10m` | `1m` aggregation | 1 year |
| Higher timeframe | `1D` | Alpaca daily bars | 5 years |
| Higher derived | `1W`, `1M` | `1D` aggregation | 5 years |

V1에서는 derived interval을 query-time aggregation으로 제공한다. 장기적으로는 모든 interval candle을 `chart_candles`에 materialized 저장한다.

Direct Alpaca backfill은 `1m`, `1D`만 수행한다. 사용자가 `5m`, `10m`, `1W`, `1M`을 먼저 열어 backfill이 필요해지면 API는 내부적으로 source interval backfill로 전환한다. 즉 `5m/10m` 요청은 `1m`, `1W/1M` 요청은 `1D` request를 생성하고, 응답에는 requested interval과 source interval을 함께 남긴다.

자동 backfill range는 `.env` override가 아니라 위 interval policy를 따른다. 명시적인 API `start/end`는 그대로 존중하지만, 숨은 default lookback 설정이 1년/5년 정책을 바꾸면 안 된다.

## Aggregation Policy

- `5m`, `10m`: stored `1m` candle에서 UTC bucket 기준 OHLCV를 집계한다.
- `1W`: stored `1D` candle에서 UTC Monday-start week 기준 OHLCV를 집계한다.
- `1M`: stored `1D` candle에서 UTC calendar month 기준 OHLCV를 집계한다.
- 집계 결과도 `ma5`, `ma20`, `ma60` flat field를 포함해야 한다.
- 데이터가 없으면 false candle을 만들지 않고 empty/backfill 상태를 반환한다.

## Verification

- `git diff --check`
- `npm run test:chart --prefix apps/gops-frontend`
- `npm run build --prefix apps/gops-frontend`
- `.venv/bin/python -m compileall packages services/07-api-websocket/gops-backend/app tests`
- `.venv/bin/python -m unittest discover -s services/07-api-websocket/gops-backend/tests`
- `env PYTHONPATH=packages:services/07-api-websocket/gops-backend .venv/bin/python -m unittest discover -s tests`
- 브라우저에서 timeframe `1m`, `5m`, `10m`, `1D`, `1W`, `1M` 선택과 MA line 렌더링을 확인한다.
