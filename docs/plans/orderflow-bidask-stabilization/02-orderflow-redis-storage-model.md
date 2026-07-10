# 02. 오더플로우 Redis 저장 모델: 캔들형 "append + 덮어쓰기" 전환

## 목표

오더플로우 라이브 데이터의 Redis 사용을 캔들과 같은 모델로 바꾼다:

```text
마감된 1분 버킷  → 불변 블롭 1개를 분당 1회 append
진행 중인 1분    → 블롭 1개를 덮어쓰기 (스로틀, 초당 최대 4회)
```

체결량과 무관하게 심볼당 Redis 쓰기 횟수가 상수가 되고, 읽기도 수만 필드 HGETALL에서
소수 명령으로 줄어든다. **API/WS 계약은 변경하지 않는다** — 내부 저장과 provider만
바뀌므로 프론트(01)와 독립 배포 가능하다.

## 현재 구조 (변경 대상)

- 쓰기: 체결마다 분류 → bin 갱신 → `write_order_flow_bin_to_redis`가
  `order-flow:{symbol}:live` 해시에 `{eventMinute}|{priceBin}` 필드 HSET + EXPIRE
  (`alfaka/streaming/processor.py:596-614, 965-977`). NVDA 피크에 초당 수백 HSET.
- 누적: 해시 하나에 세션 전체 분×$0.01빈 필드가 쌓임 (NVDA 하루 수만 필드, TTL 86400,
  세션 롤오버 시 DEL).
- 읽기: intraday API가 해시 전체를 읽어 분별로 그룹핑
  (`app/market_data/query/service.py:368-408`, provider `order_flow_live_bins`).
- WS: `maybe_publish_order_flow_event`가 250ms 스로틀로 분 bins를 발행
  (`processor.py:979-1002`) — 이 부분은 이미 건전하므로 유지.

## 변경 사양

### A. 새 Redis 레이아웃

```text
order-flow:{symbol}:minutes      ZSET
  score  = 분 시작 epoch seconds
  member = JSON 블롭 {"eventMinute": "...", "sessionDate": "...",
                       "bins": [{priceBin, askVolume, bidVolume, unknownVolume,
                                 askTradeCount, bidTradeCount, unknownTradeCount}, ...]}
  TTL 86400, 세션 롤오버 시 DEL. 멤버 수 상한 = 정규장 390.

order-flow:{symbol}:live-minute  STRING (JSON 블롭, 위와 동일 형태)
  덮어쓰기 전용. TTL 300.
```

기존 `order-flow:{symbol}:live` 해시는 전환 완료 후 자연 만료로 소멸한다(§D).

### B. 프로세서 쓰기 경로

`process_order_flow_live_path`(`processor.py:596-614`)에서:

1. 분류·bin 갱신은 현행 유지 (`OrderFlowBinBuilder`는 이미 분·빈 상태를 메모리에
   보유, `alfaka/orderflow/bins.py`).
2. `write_order_flow_bin_to_redis`(체결마다 HSET)를 제거하고 **flush 스케줄**로 교체:
   - 진행 분: 마지막 flush로부터 `ORDER_FLOW_REDIS_FLUSH_MS`(env, 기본 250) 경과 시
     현재 분 블롭 전체를 `live-minute`에 SET(EX 300 포함, 명령 1개).
   - 분 마감(다음 분의 첫 체결 감지 또는 기존 minute-close 경로): 마감 분 블롭을
     `minutes` ZSET에 ZADD 1회 + TTL refresh(분당 1회) + `live-minute`를 새 분으로
     교체. **마감 직전 상태가 반드시 ZADD에 반영**되도록 마감 시 강제 flush.
   - 세션 롤오버: 두 키 모두 DEL (기존 `consume_session_rollover` 지점 이전과 동일).
3. WS 발행(`maybe_publish_order_flow_event`)은 builder 메모리에서 직접 만들므로
   변경 없음. 발행 스로틀 250ms와 flush 250ms가 자연 정렬된다.

효과: 심볼당 쓰기 = SET ≤4/s + ZADD 1/min + TTL refresh 1/min. 체결량 무관.

### C. API provider 읽기 경로

- `order_flow_live_bins`(redis provider)를 새 레이아웃으로 교체:
  `ZRANGEBYSCORE order-flow:{s}:minutes -inf +inf` + `GET live-minute` → 분 리스트로
  변환. **`order_flow_intraday`의 응답 JSON(`minutes[]`, `liveQuote`, 메타 필드)은
  바이트 단위로 동일하게 유지**한다 (`service.py:368-408`은 그룹핑 로직이 단순해질
  뿐 출력 불변).
- live-minute의 분이 ZSET 마지막 멤버와 같은 분이면 live-minute이 우선한다(더 최신).

### D. 배포 순서와 호환 창

1. release 1: API provider가 **새 키 우선, 비면 구 해시 폴백**으로 읽기 배포.
2. release 2: 프로세서가 새 레이아웃으로 쓰기 시작 (장중 배포 시 그 시점까지의 분은
   구 해시, 이후 분은 새 키에 있게 됨 — 폴백 병합은 하지 않고, 전환 당일 하루만
   과거 분 일부 누락을 허용한다. 원하면 개장 전 배포로 회피).
3. release 3 (다음 정기 배포): 구 해시 폴백 제거. 구 키는 TTL로 소멸.

### E. `/api/charts/order-flow/daily` deprecated 처리

- route·응답은 그대로 유지한다 (AGENTS.md API 보존 규칙). 이 계획 폴더와
  `docs/CHART_DATA_REBUILD_PLAN.md`의 order-flow 단락에 "차트는 더 이상 daily를
  사용하지 않음, 검증·감사용" 주석을 추가한다.
- EOD 롤업 크론잡은 유지한다 (README 결정 기록 참조 — 03 검증의 기준선).

## 수용 기준

1. 장중 프로덕션에서 `order-flow:*` 관련 Redis 명령 빈도가 체결량과 무관하게
   심볼당 SET ≤4/s + 분당 ZADD 1회로 관측된다 (04 §1 commandstats로 확인).
2. `GET /api/charts/order-flow/intraday` 응답이 전환 전과 동일 스키마·동등 데이터
   (같은 분 입력 기준 diff 없음 — 단위 테스트로 구/신 provider 출력 비교).
3. WS `ORDER_FLOW_BINS_UPDATE` 동작 불변.
4. 세션 롤오버 시 두 키가 정리되고 다음 세션이 깨끗하게 시작된다.
5. `systems/market-data/tests` + `systems/api-server/tests` 통과, flush·minute-close
   강제 flush·롤오버에 대한 fake-clock 단위 테스트 추가.

## 파일 목록

- `systems/market-data/shared/alfaka/streaming/processor.py` (flush 스케줄러, 쓰기 교체)
- `systems/market-data/shared/alfaka/orderflow/bins.py` (dirty/마감 상태 노출 필요 시)
- `systems/market-data/shared/alfaka/orderflow/config.py` (`ORDER_FLOW_REDIS_FLUSH_MS`)
- redis key 정의 모듈 (`redis_keys` — 새 키 2개)
- `systems/api-server/pods/api-server/gops-backend/app/market_data/` redis provider +
  `query/service.py` (읽기 경로, 출력 불변)
- `docs/CHART_DATA_REBUILD_PLAN.md` (order-flow 단락의 Redis 키·daily 사용처 주석 갱신)
