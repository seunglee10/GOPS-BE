# 04. Redis 경량화: quote/trade 핫패스 감축

## 목표

Bid/Ask·오더플로우 도입 후 Redis가 버티지 못하는 상태를 해소한다. 핀 5종목과 기능은
유지하면서, **메시지당 Redis 명령 수와 pub/sub 발행량을 구조적으로 줄인다.** Redis에는
"프론트가 지금 필요로 하는 최신 상태"만 남기고, 나머지는 메모리(프로세스 내)나
ClickHouse에 둔다.

## 현재 부하 구조 (측정 근거)

`alfaka/streaming/processor.py` 기준, **quote 1건당** (스로틀 없음):

| 호출 | Redis 명령 | 위치 |
| --- | --- | --- |
| `write_quote_to_redis` | SET + EXPIRE = 2 | `processor.py:782-785` |
| `publish_chart_event(quote_event)` | PUBLISH ×2 (심볼 채널 + 전역 `market.events`) | `processor.py:540-541, 1009-1012` |
| `write_processor_health` | 컴포넌트 키 + 심볼별 키 HSET 등 ≥2 | `processor.py:534, 542, 1093-1118` |

→ quote 1건 ≈ **6+ 명령**. SIP에서 NVDA급 종목은 피크에 초당 수천 quote이므로, 핀
5종목 + 활성 차트 심볼만으로도 수만 ops/sec가 된다. 이것이 주 과부하원이다.

**trade 1건당** (핀 심볼): `write_trade_to_redis` HSET(9필드)+EXPIRE, 오더플로우 bin
HSET+EXPIRE(체결마다, `processor.py:965-977`), `PinnedQuoteCache` GET(150ms 메모이즈),
health ×2, live candle 관련 쓰기. pub/sub만 250ms 스로틀되어 있고 **Redis 쓰기 자체는
스로틀이 없다.**

또한 API `stream_hub`는 전역 `market.events` 채널 하나만 구독하므로
(`app/market_data/realtime/stream_hub.py:111-112`) 모든 quote 이벤트를 수신·파싱한다.
소스에서 발행을 줄이면 API CPU도 함께 준다.

## 변경 사양

각 항목은 독립 적용 가능하다. §1은 측정, §2~§4는 무손실 감축(기능 동일), §5~§6은
구조 개선이다.

### §1. Baseline 대체 측정 (코드 변경 전 프로덕션 측정 미수행)

코드 변경 전 프로덕션 Redis commandstats 스냅샷은 수행하지 않았고, 이 구현 작업에서도
프로덕션 접근을 시도하지 않는다. 효과 검증은 다음 두 가지로 대체한다.

1. 코드 레벨 Redis 명령 수 테스트: spy/fake Redis 클라이언트와 fake clock으로 quote,
   trade/order-flow, health 경로의 메시지당 Redis 명령 수가 스로틀 창 또는 flush 간격
   단위 상수로 수렴함을 증명한다. 실제 시장 데이터를 로컬 런타임에 주입하지 않고
   테스트 더블만 사용한다.
2. 배포 후 장중 Redis commandstats 1회 측정: 운영자가 같은 배포 환경에서 아래 명령을
   실행하고 이 문서의 "After 기록" 섹션에 붙여넣는다.

```text
redis-cli INFO commandstats   # calls/usec per command
redis-cli INFO stats          # instantaneous_ops_per_sec, connected_clients
redis-cli INFO memory
```

### §2. SET+EXPIRE 결합 및 파이프라인화

- `write_quote_to_redis`: `SET key val EX 300` 한 명령으로 결합 (redis-py
  `set(key, val, ex=300)`). `write_event_to_redis` 동일.
- 한 메시지 처리에서 발생하는 다중 명령(HSET+EXPIRE 등)은 redis-py `pipeline()`으로
  묶어 왕복(RTT)을 줄인다. 명령 수 자체도 EXPIRE 분리 호출 제거로 감소.

### §3. 쓰기·발행 스로틀 (심볼별, env 튜너블)

새 env(기존 `alfaka/orderflow/config.py` 패턴을 따름, 모두 기본값으로 동작):

```text
QUOTE_REDIS_WRITE_MIN_INTERVAL_MS   기본 100   # live:quote:{s} SET 최소 간격
QUOTE_EVENT_PUBLISH_MIN_INTERVAL_MS 기본 250   # QUOTE_UPDATE pub/sub 최소 간격
TRADE_REDIS_WRITE_MIN_INTERVAL_MS   기본 250   # live:trade:{s} 쓰기 최소 간격
HEALTH_WRITE_MIN_INTERVAL_MS        기본 1000  # write_processor_health 간격
```

- 구현: 심볼별 last-write monotonic 타임스탬프 dict(오더플로우
  `order_flow_publish_state`와 같은 패턴). **최신값 우선(latest-wins)** — 스로틀 구간
  내 마지막 값이 다음 쓰기에 반영되도록 스킵이 아니라 지연 병합으로 처리하거나,
  단순 스킵이라면 간격을 100ms 이하로 유지해 표시 지연을 사람 눈에 무의미하게 한다.
- `write_processor_health`는 메시지마다 전역+심볼별 2키를 쓰는 현재 구조를
  간격 스로틀로 감싼다. 진단 가치가 유지되는 1초 간격이면 충분하다.
- **결합 규칙(README 참조):** `QUOTE_REDIS_WRITE_MIN_INTERVAL_MS > 100`으로 올리는 것은
  §5(인메모리 NBBO) 적용 후에만 허용. 라이브 분류가 `live:quote`를 읽는 동안에는
  쓰기 지연이 곧 분류 지연이기 때문이다.
- 기대 효과: quote 경로 명령 수 ~95% 이상 감소 (초당 수천 → 심볼당 초당 ~10 + 발행 4).

### §4. 심볼별 pub/sub 채널 발행 정리

`publish_chart_event`는 심볼 채널과 전역 채널에 이중 발행한다
(`processor.py:1009-1012`). API stream_hub는 전역 채널만 구독한다.

- 먼저 심볼별 채널(`market_events_symbol`)의 구독자를 전수 조사한다
  (api-server, agent-orchestration, jobs, scripts 전체 grep). 프로덕션 PUBSUB 확인은
  이 작업의 선행조건이 아니며, 필요하면 배포 후 운영자가 확인한다.
- repo grep 결과 API stream hub는 전역 `market.events`만 구독하고, 심볼별 채널
  구독자는 없다. 따라서 심볼별 발행을 제거한다(발행량 절반).

### §5. 핀 심볼 분류용 인메모리 NBBO (구조 개선, 권장)

라이브 오더플로우 분류가 Redis를 거치지 않게 한다:

- trade를 처리하는 `alfaka-market-processor`가 `market.input.realtime.quotes.v1`을
  **추가로, 별도 consumer group 없이(assign 또는 전용 group)** 구독해 핀 5종목의
  quote만 걸러 프로세스 내 최신 NBBO(심볼당 1개, 또는 최근 N개 ring buffer)를
  유지한다.
- `process_order_flow_live_path`(`processor.py:596-614`)의 `PinnedQuoteCache.quote_for`
  호출을 인메모리 조회로 교체. Redis GET(체결마다) 제거 + 호가 신선도는 Kafka 소비
  지연 수준으로 개선.
- ring buffer로 유지하는 경우 as-of 조회(체결 timestamp 이하 최신 호가)가 가능해져
  03 판정 B의 수리까지 자연 확장된다. **v1은 최신 1개로 시작**하고, as-of 확장은 03
  판정 후 결정한다.
- 주의: quote 전담 `alfaka-market-quote-processor`는 그대로 둔다(레이어 발행·Redis
  최신 호가 유지 책임). 이 변경은 trade 프로세서의 "분류용 읽기 경로"만 바꾼다.
- 메모리 비용: 핀 5종목 × NBBO 1개(또는 ring 수천 개) — 무시 가능.

### §6. 오더플로우 bin 쓰기 — `02-orderflow-redis-storage-model.md`로 대체됨

초기 계획(dirty bin HSET 코얼레싱)은 저장 모델 자체를 캔들형(마감 분 블롭 append +
진행 분 덮어쓰기)으로 바꾸는 02 문서로 대체되었다. 02가 이 항목의 목표(체결량과
무관한 상수 쓰기)를 더 근본적으로 달성하므로 이 문서에서는 구현하지 않는다.

### §7. (부수) 비핀 종목 quotes 구독 상한 분리

`ALPACA_MAX_TRADE_SYMBOLS`가 quotes에도 동일 적용된다
(`alfaka/alpaca/websocket_collector.py:537-541`). 별도 env
`ALPACA_MAX_QUOTE_SYMBOLS`(기본: trade 상한과 동일)로 분리해 의도를 명시하고,
활성 차트 심볼의 quote가 잘려 unknown이 폭증하는 상황을 로그로 드러낸다.
우선순위 낮음 — §1~§6 완료 후 여력 시 진행.

## 하지 않는 것

- Redis 키 스키마·TTL 계약 추가 변경. 02 문서의
  `order-flow:{s}:minutes` closed minute blobs와
  `order-flow:{s}:live-minute` current minute 계약을 유지한다.
- Kafka 토픽/파티션 계약 변경. §5는 소비자 추가일 뿐 발행 계약은 그대로.
- 프론트 WS 이벤트 타입/페이로드 변경. 빈도만 낮아진다(LIVE_QUOTE_UPDATE ≤4/s/심볼).
- Redis 증설(maxmemory 3gb, statefulset 리소스) — 소프트웨어 감축이 먼저다.

## 수용 기준

1. 코드 레벨 Redis 명령 수 테스트가 통과하고, 배포 후 운영자가 장중 commandstats
   측정 절차를 수행할 수 있도록 이 문서의 "After 기록" 자리가 남아 있다.
2. 프론트 기능 동일: 라이브 호가 마커, 오더플로우 분 갱신(≤250ms+1분 경계),
   라이브 캔들 갱신이 모두 동작한다.
3. `systems/market-data/tests` 전체 통과 + 스로틀·코얼레싱 단위 테스트 추가
   (fake clock으로 간격 검증, minute-close 강제 flush 검증).
4. `docker compose build` 및 로컬 스모크(`scripts/local`) 통과.

## Baseline 기록

사전 프로덕션 baseline은 미수행. 아래 표는 동일 spy Redis 방식으로 기존 경로를
재현하거나 전환 전 코드 경로가 남아 있던 시점의 동작을 코드 레벨로 측정한 대체
baseline이다.

| 경로 | 측정 조건 | 변경 전 명령 수 | 변경 후 명령 수 | 증명 |
| --- | --- | ---: | ---: | --- |
| quote | AAPL quote 5건, 같은 throttle window, active-feed Redis 조회 제외 | 50 total = 10/msg (`SET` 20, `EXPIRE` 20, `PUBLISH` 10) | 5 total = live quote `SET EX` 1 + global `PUBLISH` 1 + health `SET EX` 3 | `test_orderflow_redis_lean.py::test_reproduced_legacy_quote_baseline_counts_match_documented_table`, `test_quote_path_redis_commands_are_constant_inside_throttle_window` |
| live trade | NVDA trade 5건, 같은 250ms window | 10 total = `HSET` + `EXPIRE` per trade | 2 total = `HSET` + `EXPIRE` once per symbol throttle window | `test_trade_live_state_write_is_symbol_throttled` |
| order-flow | NVDA same-window trades + 2 minute closes | live minute `SET`는 flush 간격당 1회, closed minute은 `ZADD` + `EXPIRE` per minute | live minute `SET EX`는 flush 간격당 1회, closed minute은 `ZADD` per minute + session TTL `EXPIRE` 1회 | `test_order_flow_flush_commands_do_not_scale_with_trade_count` |
| health | AAPL quote health 5회, 같은 1000ms window | 30 total = 3 health keys × (`SET` + `EXPIRE`) × 5 | 3 total = 3 health keys × `SET EX` once per health interval | `test_health_write_is_interval_throttled` |

### After 기록 (배포 후 운영자 붙여넣기)

```text
날짜/시간대:
배포 버전/commit:
instantaneous_ops_per_sec:
connected_clients:
commandstats 상위 10:
memory:
비고:
```
