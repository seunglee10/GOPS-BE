# 알림 센터 구현 계약

작성일: 2026-07-14
대상: `PriceConditionPanel`, `/api/alerts*`, alert evaluator, 인앱 알림 배너

## UI 계약

- 알림 탭은 `리마인더`와 `기업 알림` 두 섹션으로 나눈다. `1단계/2단계`, 상태 열,
  상태 문구, 행 앞 이모지는 사용하지 않는다.
- 행에는 이름과 유효기간만 먼저 보인다. 기업명을 누르면 조건과 설정 위치를 펼친다.
- 조작은 23px 종과 휴지통 아이콘만 사용한다. 아이콘 배경과 네모 테두리는 없고,
  종은 OFF 회색 선 / ON 파란 선으로만 구분한다.
- 기업 하나에 여러 조건을 허용하며 각각 별도 행으로 표시한다.
- 1회성 알림은 발화 뒤 `fired`가 되어 활성 목록에서 사라진다. 반복 알림은 사용자가
  끄거나 삭제할 때까지 남는다.

## 리마인더

| 키 | 표시 | 기본값 | 현재 발송 |
|---|---|---:|---|
| `marketOpen` | 미국장 개장 | ON | 09:20 ET, 개장 10분 전 |
| `marketClose` | 미국장 마감 | ON | 15:50 ET, 마감 10분 전 |
| `socialIssue` | 사회 이슈·논란 | OFF | 설정 저장만 지원 |
| `rsiBand` | RSI 과매수·과매도 | ON | 관심기업 일봉 RSI(14), 70/30 진입 |
| `economicCalendar` | 주요 경제지표 일정 | ON | 설정 저장만 지원 |
| `earnings` | 실적 발표 일정 | ON | 설정 저장만 지원 |
| `volumeSpike` | 거래량 급증 | OFF | 관심기업 5분봉, 최근 20봉 평균 배수 |
| `tradingHalt` | 거래 정지·재개 | ON | 설정 저장만 지원 |
| `marketVolatility` | 시장 변동성 확대 | ON | 설정 저장만 지원 |

실적 D-1 알림은 지원하지 않는다. 설정만 저장되는 항목은 이벤트 producer가 추가되기
전까지 알림을 만들지 않는다.

## 기업 조건

지원 조건은 한 알림당 하나다.

- 목표가 상향/하향 돌파
- N분 가격 변동률 이상/이하
- 봉 거래량 절대값 이상/이하
- 최근 N봉 평균 거래량 배수 이상/이하
- RSI 기준값 이상/이하 (기본 일봉 RSI(14))

기본 수명은 1회다. `repeatLimit`은 `1`, `3`, `5`, `10`, `null`(무제한)을 받는다.
반복 조건은 false에서 true로 다시 진입할 때만 재발화하며 시간 쿨다운은 사용하지 않는다.
유한 횟수 조건은 별도 만료를 주지 않으면 90일 안전 만료를 적용한다.

## API와 저장

```text
POST   /api/alerts                  구조화 조건 생성; Idempotency-Key 선택
GET    /api/alerts?includeTerminal=false
PATCH  /api/alerts/{id}             active/disabled
DELETE /api/alerts/{id}
POST   /api/alerts/commands         자연어 조건 생성; Idempotency-Key 필수
GET    /api/notification-preferences
PATCH  /api/notification-preferences
WS     /ws/notifications
```

`0010_alert_condition_rules.sql`은 `condition`, `condition_version`, `created_via`,
`request_id`, `last_triggered_at`을 추가한다. Postgres가 원본이고 Redis projection은
evaluator 조회용이다. 생성 위치는 `manual`, `chart`, `ai_coach`, `agent_chat`,
`trade_condition` 중 하나다.

## 에이전트 명령

프런트는 일반 분석 전에 `/api/alerts/commands` fast path를 호출한다. 결정론 파서가
종목·조건·임계값·봉 간격을 먼저 해석하고, 표현이 낯선 경우에만 orchestrator
`/alerts/resolve`의 strict JSON resolver를 사용한다. 필수값을 임의로 채우지 않으며
빠진 값은 `clarificationId`와 한 개의 후속 질문으로 반환한다. 답변은 10분 draft와
합쳐 다시 파싱한다.

## 평가와 전달

```text
trades + closed candles -> evaluator -> Redis Stream outbox
                         -> notification + alert trigger DB transaction
                         -> Redis notify:{userSub} -> /ws/notifications -> toast
                         -> alerts.triggered.v1
```

캔들 조건은 1m/5m/10m/1h/4h/1D closed topic을 구독한다. evaluator 시작 시 활성
조건과 관심기업에 필요한 최대 240봉을 ClickHouse에서 채운다. WebSocket snapshot의
안 읽은 알림도 오래된 순서로 toast queue에 복구한다. 조건 생성 자체는 toast를
띄우지 않고, 조건이 실제 발화할 때만 배너를 띄운다.
