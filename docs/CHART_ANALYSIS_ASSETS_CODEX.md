# Chart Geometry Assets — Codex Reference

## 불변 조건

- 지원 interval은 `1m/5m/10m/1h/4h/1D/1W`뿐이다.
- 분석 입력은 정규장·분할조정·완료된 canonical candle뿐이다.
- `1W`는 canonical `1D`에서 기존 주봉 방식으로 생성한다.
- 모든 timed anchor는 현재 asset interval의 실제 candle timestamp에 속한다.
- 지지·저항 `horizontalLine`은 첫·마지막 접촉의 동일 가격 2-anchor를 저장하며,
  차트 엔진은 수동 작도의 기존 단일 anchor와 이 형식을 모두 허용한다.
- 작도는 지지·저항 최대 4개와 상승·하락·대칭 삼각형 선 2개뿐이다.
- geometry 계산에는 LLM을 사용하지 않는다.
- chart asset payload/job은 PostgreSQL, candle은 ClickHouse에 저장한다.
- 결측 보충은 Alpaca의 정확한 누락 range만 사용하며 S3·Redis·Kafka를 거치지 않는다.
- 동일 `(symbol, interval, inputDigest, algorithmVersion)`은 no-op이다.
- 120개 미만 또는 interior/tail gap이면 기존 성공 자산을 덮어쓰지 않는다.

## Coverage 계약

| Interval | Target | Warm-up | Evaluation | Minimum | Cross |
| --- | ---: | ---: | ---: | ---: | ---: |
| `1m`~`1D` | 380 | 120 | 260 | 120 | 121 |
| `1W` | 312 | 120 | 192 | 120 | 121 |

120봉은 SMA60/120 값을 계산할 수 있지만 직전 봉 비교가 없으므로 교차 상태는
`insufficient_previous_bar`다. SMA 기간은 달력 일수가 아니라 선택 interval의 완료
봉 개수다.

## 주요 코드 경계

- `alfaka.analytics.geometry`: OHLCV evidence, 수평선, 삼각형, SMA/교차
- `alfaka.analytics.analysis_candles`: 완료 봉과 canonical identity, 기존 주봉 집계
- `alfaka.analytics.analysis_repair`: ClickHouse audit와 Alpaca-only repair
- `gops_agents.chart_assets.builder`: symbol/interval 단위 조립과 digest no-op
- `gops_agents.chart_assets.storage`: PostgreSQL 최신 geometry 자산
- `gops_agents.chart_assets.job_store`: PostgreSQL queue, 15분 lease, 최대 2회 시도

새 payload의 `assetVersion`은 숫자 개발 단계가 아니라 기존 응답 union을 구분하는
semantic discriminator인 `geometry`다. `algorithmVersion`은 현재
`ohlcv-consensus-1`이며 분석 의미가 바뀔 때만 변경한다. 기존 숫자형 자산 row는 읽기
fallback이나 자동 변환에 사용하지 않는다.

PostgreSQL 테이블은 `geometry_assets`, `geometry_build_jobs`,
`geometry_build_items`다. 자산 기본 키는 `(symbol, interval)`이고 item claim은
`FOR UPDATE SKIP LOCKED`를 사용한다.

## 검증

```sh
.venv/bin/python -m pytest systems/market-data/tests/analytics/test_geometry_assets.py
.venv/bin/python -m pytest systems/agent-orchestration/tests/test_geometry_asset_contract.py
.venv/bin/python -m pytest systems/api-server/tests/test_chart_assets_routes.py
```

프론트는 `Geometry` 토글 하나만 제공하고, 현재 interval의 자산만 적용하며,
SMA60·SMA120 overlay를 함께 활성화한다.
