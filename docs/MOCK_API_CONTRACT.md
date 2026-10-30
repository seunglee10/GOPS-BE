# 목업 데이터 ↔ API 대응 계약

기준 목업: `gops-web-mock-learn-split/assets/gops.js` (2026-10-30 시점). 줄 번호는 이 파일 기준이다.
프론트(GOPS-FE)는 이 문서로 `src/api/types.ts`를 만들고, 백엔드는 이 문서대로 신규 엔드포인트를 구현한다.
둘 중 하나가 바뀌면 이 문서를 같은 PR에서 고친다.

표기: **있음** = 기존 엔드포인트. 프론트 훅에서 응답을 목업 형태로 바꾼다. **신규** = 이 문서의 2절에 응답 예시가 있다.
JSON 키는 camelCase, 시각은 UTC ISO 8601(`Z`), 금액은 달러 숫자(원화 환산은 프론트가 D6 환율로 한다).

## 1. 대응표

| # | 목업 데이터·행동 | 목업 위치 | 엔드포인트 | 상태 |
|---|---|---|---|---|
| D1 | 종목 마스터 `S` (한글명·거래소·현재가·등락·시총) | 8~26 | `GET /api/market/symbols`, `/symbols/{symbol}`, `/symbols/search`, `/heatmap` items | 있음 + 신규 필드 `nameKo`, `exchangeKo` |
| D2 | 섹터·트리맵 `SECTORS` | 28~40 | `GET /api/market/heatmap?universe=sp500` | 있음 |
| D3 | 지수 묶음 + 스파크라인 `MKT` | 723~739 | `GET /api/market/indices` | 있음 + 신규 심볼(`KRW=X`, `^TNX`, `^VIX`)·필드 `nameKo`, `sparkline` |
| D4 | 장 상태 문구 | 홈 상단 | `GET /api/market/status`, `GET /api/market/next-open` | 있음 |
| D5 | 홈 순위표 `TRADE`·`WHY`·`CHART` | 650~720 | `GET /api/stocks/movers` | 신규 |
| D6 | 환율 `FX` | 6 | `GET /api/market/fx` | 신규 |
| D7 | 캔들 `candlesOf`, `cxSeries` | 189, 2282~2343 | `GET /api/charts/candles` | 있음. 3·15·30분봉은 프론트가 1분·5분봉에서 집계 |
| D8 | 실시간 시세·체결·호가 `LIVE` | 1803~1830 | `WS /ws/charts` | 있음. 호가는 최우선 1단만 |
| D9 | 종목 지표 `factsOf`, `PEERS` | 969, 1223 | `GET /api/stocks/{symbol}/summary` | 신규 |
| D10 | 분기 매출·수익성 표 | 2042~2053 | `GET /api/market/fundamentals/{symbol}/series` | 있음 |
| D11 | 지지·저항, AI 목표가·손절가 `levelsOf` | 206 | `GET /api/charts/analysis-assets` | 있음 |
| D12 | 실적일·차트 이벤트 `chartEventsOf`, `UPCOMING` | 980~1016 | `GET /api/charts/events`, `GET /api/market/fundamentals/{symbol}/earnings` | 있음 |
| D13 | 뉴스 `NEWS` (분류·관련 종목·본문) | 2079~2157 | `GET /api/market/news/latest`, `/daily`, `/watchlist`, `GET /api/news/{id}` | 있음 + 신규 필드 `category`, 신규 본문 엔드포인트 |
| D14 | 종목 AI 요약 `AI_SUM` | 1084~1139 | `GET /api/stocks/{symbol}/ai-summary` | 신규 |
| D15 | 오늘 시장 요약 | 홈 | `GET /api/market-brief/today` | 신규 |
| D16 | AI 대화 | 337~481 | `POST /api/agents/analyze`, SSE `GET /api/agents/reports/{id}/stream`, `GET /api/agents/threads` | 있음 + 신규 대화 목록·`view` 필드 |
| D17 | 관심 종목 `WATCH`, `star` | 380, 3296 | `GET/PUT /api/charts/watchlist` | 있음 |
| D18 | 최근 본 종목 `RECENT` | 407, 941 | `GET/POST /api/me/recent-symbols` | 신규 |
| D19 | 종목 검색 | 345~363 | `GET /api/market/symbols/search`, `GET /api/charts/hot-symbols` | 있음 |
| D20 | 보유·현금·KPI·성과·자산 구성·최근 활동 | 2862~2953 | `GET /api/paper/account`, `/api/account/holdings`, `/api/account/performance`, `GET /api/account/activity` | 있음 + 신규 활동 목록 |
| D21 | 주문 | 542~572, 1884 | `POST /api/paper/orders`, `POST /api/paper/risk/pretrade` | 있음 + 신규 필드 `memo` |
| D22 | 주문 내역·취소 | 2877 | `GET /api/paper/orders?status=&page=&size=`, `POST /api/paper/orders/{id}/cancel` | 있음 + 필터·페이지 파라미터 |
| D23 | 규칙 `RULES` | 42~48, 574~593 | `GET/POST/PATCH/DELETE /api/alerts`, `/api/trade-conditions` | 있음 + 신규 필드 `source` |
| D24 | 빠른 가격 알림 | 1841~1845 | `POST /api/alerts` | 있음 |
| D25 | 알림함 | 506~515 | `GET /api/notifications`, `/unread-count`, `PATCH /read-all`, `WS /ws/notifications` | 있음 |
| D26 | 가드레일 g1·g3·g4·비중 25% | 3083~3087 | `GET/PATCH /api/risk/settings` | 있음 + 신규 키 3개 |
| D27 | 투자 복기 | 2956~3090 | `GET /api/review/monthly`, `PUT /api/review/notes/{orderId}` | 신규 |
| D28 | 경제·실적 일정, 주간 AI 요약 | 759~793 | `GET /api/calendar`, `GET /api/calendar/brief` | 신규 |
| D29 | 코스 문제·실제 시세 `COURSE`, `COURSE_CHARTS` | 1408~1491, course-data.js | `GET /api/learn/courses/{courseId}` | 신규 |
| D30 | 코스 진행 | 1472 | `GET/POST /api/learn/progress` | 신규 |
| D31 | 용어 풀이 `termCard` | 1224~1366 | 없음 (프론트 정적) | — |
| D32 | 투자 경험 `gops.exp` | 610~617 | `GET/PUT /api/recommendations/profile` | 있음 (`experience` 매핑) |
| D33 | 오늘의 추천 | 홈 | `GET /api/recommendations/stocks/latest` | 있음 |
| D34 | 화면 설정 동기화 | 1777, 2263~2271 | `GET/PUT /api/me/preferences` | 신규 |
| D35 | 이메일 로그인 | 3196, 3544 | `POST /api/auth/email/login` | 신규 |
| D36 | 회원가입 | 3213, 3550 | `POST /api/auth/email/signup` | 신규 |
| D37 | 이메일 인증 | 3233, 3551 | `POST /api/auth/email/verify`, `POST /api/auth/email/resend` | 신규 |
| D38 | 비밀번호 재설정 | 3259, 3553 | `POST /api/auth/password/forgot`, `POST /api/auth/password/reset` | 신규 |
| D39 | Google 첫 가입 약관 | 3248, 3552 | `GET /api/auth/me` (`needsConsent`), `POST /api/auth/consent` | 있음 + 신규 |
| D40 | 로그아웃·게스트 | 3339 | `POST /api/auth/logout`, `GET /api/auth/me` | 있음 |

### 데이터가 없어서 축소한 항목
| 목업 | 현실 | 처리 |
|---|---|---|
| 국내 종목 `KR_S`, `mk=kr` | S&P 500만 있음 | v1 범위 밖. 프론트는 국내 탭을 비활성 표시 |
| 호가 16단 `LIVE.book` | Alpaca 최우선 호가 1단 | `WS /ws/charts` quote 이벤트의 bid/ask 1단만 |
| 기관·외국인·개인 순매수, 공매도, 내부자 | 미국 종목 데이터 없음 | 응답에 없음. 프론트는 칸을 "데이터 없음"으로 |
| 뉴스 1시간 조회 수 `NEWS_STAT` | 없음 | 제공하지 않음 |

## 2. 신규·확장 엔드포인트 응답

### D1 종목 한글명
`GET /api/market/symbols/NVDA`
```json
{ "symbol": "NVDA", "companyName": "NVIDIA Corp", "nameKo": "엔비디아", "exchange": "NASDAQ", "exchangeKo": "나스닥", "sector": "Information Technology", "sectorLabelKo": "정보기술" }
```
시드에 없는 종목은 `nameKo: null`, `exchangeKo: null`. `/heatmap` items와 `/symbols/search` items에도 같은 두 필드가 붙는다.

### D3 지수
`GET /api/market/indices`
```json
{ "updatedAt": "2026-10-30T20:00:00Z", "items": [
  { "symbol": "^GSPC", "nameKo": "S&P 500", "value": 5812.4, "change": 35.9, "changePercent": 0.62, "sparkline": [5760.1, 5771.3, "...30개"] },
  { "symbol": "^IXIC", "nameKo": "나스닥 종합", "value": 18420.15, "change": 191.2, "changePercent": 1.05, "sparkline": [] },
  { "symbol": "^DJI", "nameKo": "다우존스" }, { "symbol": "^VIX", "nameKo": "VIX" },
  { "symbol": "KRW=X", "nameKo": "원/달러" }, { "symbol": "^TNX", "nameKo": "미 10년물" }
] }
```
`sparkline`은 최근 종가 30개(오래된 것부터). 자료가 없으면 빈 배열.

### D5 홈 순위표
`GET /api/stocks/movers?sort=trade|volume|up|down&limit=20`
```json
{ "asOf": "2026-10-30T20:00:00Z", "items": [
  { "symbol": "NVDA", "nameKo": "엔비디아", "price": 142.3, "changePercent": 2.12, "dollarVolume": 31200000000, "volume": 210000000,
    "volumeRatio20d": 2.1, "issue": "데이터센터 수요 전망 상향", "issueNewsId": "n_123", "chartNote": "20일선 위에서 거래량 늘며 상승" }
] }
```
`issue`·`chartNote`는 D14 AI 요약 캐시에서 가져온 한 줄. `issue`는 D14 `issueLine`을 쓰고, 예전 캐시처럼 `issueLine`이 없으면 `head` 첫 문장을 24자로 자른다(넘치면 `…`). 캐시가 없으면 `null`.

### D6 환율
`GET /api/market/fx?pair=USDKRW`
```json
{ "pair": "USDKRW", "rate": 1382.5, "asOf": "2026-10-30T09:00:00Z", "source": "yahoo-finance" }
```

### D9 종목 요약
`GET /api/stocks/{symbol}/summary`
```json
{ "symbol": "NVDA", "nameKo": "엔비디아", "exchangeKo": "나스닥", "price": 142.3, "change": 2.95, "changePercent": 2.12,
  "marketCap": 3480000000000, "marketCapRank": 1, "per": 54.2, "sectorPer": 31.5, "sectorLabelKo": "정보기술",
  "low52": 75.61, "high52": 152.89, "dayLow": 139.2, "dayHigh": 143.1,
  "volume": 210000000, "avgVolume20d": 100000000, "volumeRatio20d": 2.1, "dollarVolume": 31200000000,
  "peers": [ { "symbol": "AMD", "nameKo": "AMD", "per": 42.1 }, { "symbol": "AVGO", "nameKo": "브로드컴", "per": 35.0 } ],
  "asOf": "2026-10-30T20:00:00Z" }
```
PER은 현재가 ÷ 최근 12개월 EPS. EPS는 `sec_derived_metrics`에 없어서 `sec_financial_facts`의 `eps`(최근 4개 분기 합, 최근 연간 값이 더 새로우면 연간 값)를 쓴다. EPS가 없거나 0 이하면 `null`. `sectorPer`는 같은 섹터 시총 상위 30개 중 PER이 있는 종목의 중앙값(없으면 `null`). `peers`는 같은 섹터에서 자기 자신을 뺀 시총 상위 3개. 모르는 종목은 404 `{ code: "symbol_not_found" }`. 응답은 60초 캐시.

D5·D9(`/api/stocks/`)는 현재 시세를 쓰므로 시뮬레이터 모드에서는 `/api/market/`처럼 409 `simulation_data_unavailable`.

### D13 뉴스
`GET /api/market/news/latest?category=market|corp|earn|rate|etf|global&symbols=NVDA,AMD&limit=20`
items에 `category`, `relatedSymbols: [{symbol, nameKo}]`, `publishedAt`, `source`, `titleKo`, `isHot`(관련 종목 거래량 배수 ≥ 1.5)이 포함된다.
`GET /api/news/{id}`
```json
{ "id": "n_123", "titleKo": "...", "source": "Reuters", "publishedAt": "...", "url": "...", "category": "corp",
  "relatedSymbols": [ { "symbol": "NVDA", "nameKo": "엔비디아" } ], "bodyKo": ["문단1", "문단2"] }
```
`category`는 수집 단계에서 키워드 규칙(`market_data/news/categorize.py`)으로 붙인다. 값이 비어 있는 과거 기사는 읽을 때 같은 규칙으로 계산한다. `symbol`·`symbols`가 모두 없으면 인기 종목(급등·급락·거래대금) 뉴스로 채운다.
`bodyKo`는 기사 전문 번역이 아니다. 저장된 한국어 요약(`localized_summary`)과 핵심 포인트(`key_points`)를 문단으로 나눈 것이다. 없는 id는 404 `{ "detail": { "code": "news_not_found" } }`, 시뮬레이션 중에는 409.

### D14 종목 AI 요약
`GET /api/stocks/{symbol}/ai-summary`
```json
{ "symbol": "NVDA", "generatedAt": "...", "head": "데이터센터 수요가 다시 확인되며 거래량이 늘었어요",
  "issueLine": "데이터센터 수요 전망 상향",
  "news": [ { "text": "...", "newsId": "n_123" } ], "risks": [ { "text": "...", "newsId": "n_124" } ],
  "events": [ { "title": "실적 발표", "at": "2026-11-20", "note": "..." } ], "chartNote": "20일선 위에서 거래량 늘며 상승" }
```
뉴스가 적재될 때 종목별로 미리 생성해 Redis에 저장한다(목업 1083 주석과 같은 설계). 없으면 404 `{"detail":{"code":"summary_not_ready"}}`. `issueLine`은 홈 순위표(D5) 한 줄용 명사형 구절(24자 이내, 문장 끝맺음 없음, 목업 `WHY`와 같은 모양). 생성 방식은 `STOCK_SUMMARIES.md`.

### D15 오늘 시장 요약
`GET /api/market-brief/today`
```json
{ "marketDate": "2026-10-30", "generatedAt": "...", "paragraphs": [ { "text": "반도체가 지수 상승을 이끌었어요.", "refs": [ { "kind": "symbol", "value": "NVDA" } ] } ] }
```
`refs[].kind`는 `symbol`(티커)·`news`(뉴스 id)·`macro`(지수 심볼, 예: `^GSPC`). 오늘(미국 동부 기준) 요약이 없으면 3일 안의 가장 최근 요약을 `marketDate`와 함께 돌려주고, 그것도 없으면 404 `{"detail":{"code":"brief_not_ready"}}`.

### D16 AI 대화
기존 `POST /api/agents/analyze` 요청에 선택 필드 `context: { page, symbol, chips: [] }`와 `threadId`를 추가로 받는다.
- `context.symbol`은 요청 `symbol`이 비었을 때만 채운다. `page`·`chips`는 `uiContext.pageContext`로 에이전트에 넘긴다. 모르는 키가 있으면 422.
- `threadId`(영숫자로 시작, 64자 이내)가 없으면 서버가 `thr_<uuid>`를 만든다. 응답에 `threadId`가 실린다.

리포트 응답(`POST /api/agents/analyze` 완료 응답, `GET /api/agents/reports/{id}`, SSE `report` 이벤트)에 `view` 필드를 추가한다. 리포트의 기존 필드에서 결정적으로 만들며, 아직 내용이 없는 대기 상태 리포트에는 없다.
```json
{ "view": { "conclusion": "...", "sections": [ { "title": "거래량", "body": "...", "evidence": [ { "kind": "news", "ref": "n_123" }, { "kind": "chart", "ref": "volume" } ] } ],
  "kv": [ { "k": "20일 평균 대비 거래량", "v": "2.1배" } ], "steps": [ "질문 이해", "뉴스 4건 확인", "차트 해석" ], "confidence": "높음",
  "suggestions": [ { "kind": "rule", "label": "137.70 아래로 가면 알리기", "payload": { "symbol": "NVDA", "direction": "below", "triggerPrice": 137.7, "proposalId": "..." } },
                   { "kind": "compare", "label": "NVDA · AMD 비교", "symbols": ["NVDA","AMD"], "payload": { "symbols": ["NVDA","AMD"] } },
                   { "kind": "watch", "label": "[[NVDA]] 관심 종목에 추가", "payload": { "symbol": "NVDA" } } ] } }
```
- `sections`: 분석 역할별(차트·뉴스·재무·시장 흐름·기업 관계·위험 점검·특이 움직임 순). `body`는 평문이고 RichText `**굵게**`, `[[TICKER]]`만 쓴다. HTML은 없다.
- `evidence.kind`: `news`(ref = 뉴스 id, D13 `GET /api/news/{id}`), `chart`(ref = 지표 이름), `fin`(ref = 재무 항목). 자료가 없던 근거는 싣지 않는다.
- `confidence`: 리포트 신뢰도 0.7 이상 `높음`, 0.45 이상 `보통`, 그 밖 `낮음`. 일부 자료가 빠졌으면 `높음`은 `보통`으로 낮춘다.
- `suggestions`: `rule`은 리포트의 가격 조건 제안에서, `compare`는 함께 언급된 종목에서, `watch`는 질문 종목에서 만든다. 사고팔라는 문구나 앞으로의 주가 전망은 넣지 않는다.

`GET /api/agents/threads` → `{ items: [ { threadId, title, symbol, updatedAt } ] }` (최근 50개, 최신순, `title`은 첫 질문 40자)
`GET /api/agents/threads/{threadId}` → `{ threadId, title, symbol, updatedAt, messages: [ { question, analysisId, status, view } ] }`. 남의 대화나 없는 대화는 404 `{"detail":{"code":"thread_not_found"}}`. 리포트가 만료됐으면 `status: "missing"`, `view: null`.

### D18 최근 본 종목
`GET /api/me/recent-symbols` → `{ items: [ { symbol, nameKo, viewedAt } ] }` (최근 50개, 최신순)
`POST /api/me/recent-symbols` body `{ symbol }` → 204

### D20 계좌 최근 활동
`GET /api/account/activity?limit=20`
```json
{ "items": [ { "at": "...", "kind": "fill|rule|dividend|deposit", "symbol": "NVDA", "nameKo": "엔비디아", "text": "엔비디아 10주 매수 체결 $142.30", "ref": { "orderId": "..." } } ] }
```
`fill` 은 `paper_executions`, `rule` 은 사용자 알림 규칙(`alert.*`) 알림, `dividend`·`deposit` 은 `paper_cash_ledger` 에서 온다. 현재 계좌 세대만 싣는다.

### D21 주문 메모
`POST /api/paper/orders` body에 `memo: string | null`(200자) 추가. 주문·체결 조회 응답에도 `memo`가 실린다.

### D22 주문 내역
`GET /api/paper/orders?status=all|pending|filled|cancelled&page=1&size=20` → `{ items: [], total: 38, page: 1, size: 20 }`
상태 대응: `pending` = pending·partially_filled, `filled` = filled, `cancelled` = cancelled·rejected. 예전 `order_status`·`limit`·`include_previous` 파라미터와 `orders` 키(= `items`)는 Phase 5 정리 전까지 남겨 둔다.

### D23 규칙 출처
`/api/alerts`, `/api/trade-conditions` 항목에 `source: "agent" | "chart" | "user" | "default"` 추가. 생성 시 body로 받고, 없으면 `user`.

### D26 가드레일
`GET/PATCH /api/risk/settings`에 키 추가:
```json
{ "chaseBuyWarnPercent": 3.0, "blockAverageDownWithoutStop": true, "confirmSellOnDropPercent": 2.0, "maxPositionWeightPercent": 25 }
```
`POST /api/paper/risk/pretrade` 응답 `checks[]`에 `code: "chase_buy" | "avg_down_no_stop" | "sell_on_drop" | "position_weight"`가 추가된다.

### D27 투자 복기
`GET /api/review/monthly?month=2026-09` (로그인 필요, `month` 생략 시 미국 동부 기준 이번 달, 형식이 틀리면 422 `{"detail":{"code":"invalid_month"}}`)
```json
{ "month": "2026-09",
  "kpi": { "realizedPnl": 230.0, "evalPnl": 610.0, "returnPercent": 0.61, "winRate": 83.33, "winCount": 5, "tradeCount": 6,
           "avgWin": 126.0, "avgLoss": 20.0, "payoffRatio": 6.3, "mistakePnl": 250.0, "mistakeCount": 2, "missedProfit": 130.0 },
  "prevMonth": { "realizedPnl": 0.0, "evalPnl": 40.0, "returnPercent": 0.04, "winRate": 100.0, "tradeCount": 1, "...": "kpi 와 같은 키" },
  "benchmark": { "spxReturnPercent": 2.0, "prevSpxReturnPercent": 2.04 },
  "byHabit": [ { "habit": "plan", "labelKo": "계획한 주문", "pnl": 100.0, "count": 1, "winCount": 1, "missedProfit": 0.0 } ],
  "bySymbol": [ { "symbol": "NVDA", "nameKo": "엔비디아", "pnl": 550.0, "count": 3 } ],
  "trades": [ { "orderId": "...", "at": "2026-09-15T15:00:00Z", "symbol": "NVDA", "nameKo": "엔비디아", "side": "sell", "orderType": "limit",
                "qty": 10, "price": 120.0, "avgPriceAtSell": 95.0, "pnl": 250.0, "returnPercent": 26.32,
                "markPrice": 110.0, "evalPnl": 250.0, "evalReturnPercent": 26.32, "missedProfit": 100.0,
                "habit": "exit", "habitAuto": "exit", "habitSource": "auto", "memo": "목표가 도달", "orderMemo": "목표가 도달",
                "priceAfter": { "d1": 125.0, "d5": 119.0 } } ] }
```
- 대상: 현재 계좌 세대의 페이퍼 체결. 주문 단위로 묶고, 첫 체결 시각(미국 동부 날짜)이 그달인 주문만 싣는다. `trades` 는 시간 오름차순.
- 평균 단가: 페이퍼 보유 종목과 같은 이동평균. 체결을 처음부터 다시 쌓아 매도 직전 평균(`avgPriceAtSell`)을 구한다.
- `pnl`·`returnPercent`: 판 주문의 확정 손익. 산 주문은 `null`.
- `evalPnl`·`evalReturnPercent`: 판 주문은 `pnl`과 같고, 산 주문은 최근 종가(`markPrice`) 기준. 목업의 "판 주문은 확정 손익, 들고 있는 주문은 오늘 가격 기준" 숫자다. `byHabit.pnl`, `bySymbol.pnl`, `kpi.evalPnl`, `winRate`, `avgWin`·`avgLoss`·`payoffRatio`(손익비)는 이 값으로 계산한다.
- `returnPercent`(kpi): `evalPnl` ÷ 계좌 시작 금액 × 100. `benchmark.spxReturnPercent`: S&P 500(`^GSPC`, 없으면 `SPY`) 일봉 종가 기준 그달 수익률(전달 마지막 종가 대비).
- `mistakePnl`·`mistakeCount`: 습관이 `chase`·`avg` 인 주문의 손익 합과 건수.
- `missedProfit`: 판 주문만. max(0, 판 뒤 5거래일 종가 중 최고값 − 판 가격) × 수량. kpi·byHabit 는 그 합.
- `priceAfter.d1`·`d5`: 체결일 다음 1번째·5번째 거래일 종가. 아직 없으면 `null`.
- `memo`: 복기 메모가 있으면 그것, 없으면 주문 메모(`orderMemo`, D21).
- 체크리스트를 몇 개 채웠는지 같은 점수는 싣지 않는다.

습관 분류 규칙(서버, 위에서부터 먼저 걸리는 값): `rush` 장 시작(9:30 ET) 10분 안의 시장가 주문, `chase` 체결일 종가가 직전 거래일보다 +3% 이상 오른 날의 매수, `avg` 들고 있는 종목을 평균 단가보다 싸게(손실 중) 더 산 매수, `news` 주문 전 1시간 안에 그 종목 관련 뉴스가 있었던 주문, `exit` 확정 손익이 플러스인 매도, 그 외 `plan`. 사용자가 고른 값(`habitSource: "user"`)이 있으면 그 값이 우선이고, 서버 값은 `habitAuto` 에 남는다.
`labelKo`: plan `계획한 주문`, exit `수익 내고 판 주문`, news `뉴스 보고 바로 낸 주문`, rush `장 시작 직후 시장가 주문`, chase `급등일 추격 매수`, avg `손실 중 물타기`. `byHabit` 은 이 순서로 6개 모두 싣는다(0건 포함).

`PUT /api/review/notes/{orderId}` body `{ memo?, habit? }` → 200 `{ orderId, memo, habit, habitSource, updatedAt }`
보낸 키만 바꾼다. `memo: null`·빈 문자열은 복기 메모를 지워 주문 메모로 돌아가고, `habit: null` 은 서버 분류로 돌아간다. 메모 200자 초과 422 `memo_too_long`, 문자열이 아니면 `invalid_memo`, 습관 값이 틀리면 `invalid_habit`. 남의 주문이나 없는 주문은 404 `order_not_found`. 저장은 `order_notes` 테이블(마이그레이션 0031).

### D28 캘린더
`GET /api/calendar?from=2026-10-01&to=2026-10-31&kind=econ|earn&region=kr|us`
```json
{ "items": [
  { "id": "e_1", "date": "2026-10-10", "kind": "econ", "region": "us", "title": "9월 소비자물가(CPI)", "level": "major|watch", "at": "21:30", "done": true, "actual": "3.1%", "forecast": "3.0%", "previous": "3.2%" },
  { "id": "e_2", "date": "2026-10-29", "kind": "earn", "region": "us", "title": "마이크로소프트 실적", "symbol": "MSFT", "nameKo": "마이크로소프트", "call": "장 마감 후" }
] }
```
`GET /api/calendar/brief?week=2026-W44` → `{ week, text }`

### D29 코스
`GET /api/learn/courses/chart-basics`
```json
{ "courseId": "chart-basics", "titleKo": "차트 읽기 첫걸음", "days": [ { "day": 1, "titleKo": "캔들" } ],
  "questions": [ { "id": "q1", "day": 1, "topic": "candle", "title": "...", "kind": "choice|tap|level", "prompt": "...", "opts": [], "answer": 1, "tolerance": null, "count": null,
                   "fig": { "chart": "msft_wick", "type": null, "mark": null, "prev": null, "vol": true, "rsi": false, "level": null, "day": null }, "hints": ["...", "..."], "explanation": "..." } ],
  "charts": { "msft_wick": { "sym": "MSFT", "name": "마이크로소프트", "iv": "일봉", "rows": [["2024-01-02", 370.1, 375.0, 368.2, 374.5, 25000000]] } } }
```
문제·차트는 목업 `COURSE`(1408~1464)와 `course-data.js`를 JSON 시드로 옮긴 것이다. 함수형 정답(`ans()`)은 서버가 평가한 결과를 `answer`/`answerSet`으로 미리 넣는다.

### D30 코스 진행
`GET /api/learn/progress?courseId=chart-basics` → `{ solved: ["q1","q3"], total: 15 }`
`POST /api/learn/progress` body `{ courseId, questionId, correct }` → 200 `{ solved: [...] }`. 힌트 사용은 기록하지 않는다.

### D34 화면 설정
`GET /api/me/preferences` → `{ version: 1, updatedAt, data: { layout, chart, draw, cur, sideW, dwW, brand } }`
`PUT /api/me/preferences` body `{ version, data }` → 200. `data`는 목업 localStorage 값(`gops.layout`, `gops.chart`, `gops.draw`, `gops.cur`, `gops.side.w`, `gops.dw.w`, `gops.brand`)을 그대로 담는 JSON이다. 서버는 구조를 검사하지 않고 64KB 제한만 둔다.

### D35~D39 이메일 인증
| 요청 | 성공 | 실패 |
|---|---|---|
| `POST /api/auth/email/signup` `{ email, password, consents: { tos, privacy, age, mkt } }` | 201 `{ verificationRequired: true, resendAfterSeconds: 30 }` | 409 `{ code: "email_taken" }`, 422 `{ code: "weak_password" }`(8자·영문·숫자), 422 `{ code: "consent_required" }` |
| `POST /api/auth/email/verify` `{ email, code }` | 200 `{ user }` + 세션 쿠키 | 400 `{ code: "invalid_code", remainingAttempts: 4 }`, 410 `{ code: "expired" }`(5분) |
| `POST /api/auth/email/resend` `{ email }` | 200 `{ resendAfterSeconds: 30 }` | 429 `{ code: "too_soon", retryAfterSeconds: 12 }` |
| `POST /api/auth/email/login` `{ email, password, remember }` | 200 `{ user }` + 세션 쿠키(remember면 30일) | 401 `{ code: "invalid_credentials", remainingAttempts: 3 }`, 423 `{ code: "locked", lockedUntil }`(5회, 10분), 403 `{ code: "unverified" }` |
| `POST /api/auth/password/forgot` `{ email }` | 200 `{ sent: true }`(없는 이메일도 200) | — |
| `POST /api/auth/password/reset` `{ token, password }` | 200 `{ reset: true }` 그 사용자의 세션 전부 삭제 | 400 `{ code: "invalid_token" }`, 410 `{ code: "expired" }`(30분) |
| `POST /api/auth/consent` `{ tos, privacy, age, mkt }` | 200 `{ needsConsent: false }` | 422 |

`GET /api/auth/me`의 `user`에 `provider: "email"`, `emailVerified`, `needsConsent`, `experience: "new" | "some" | "pro" | null`이 추가된다.
메일은 `MAIL_PROVIDER=console|ses`. `console`은 로그에 코드·링크를 찍는다(로컬·테스트).

## 3. 경로 규칙
- 새 경로는 `/api/market/`, `/api/agents/`, `/api/llm/` 접두사를 쓰지 않는다. 시뮬레이터 모드에서 `simulation_guard`가 409로 막는 접두사다. 예외: D3·D6은 기존 `market_data/query` 라우터 안에 있으므로 `/api/market/` 아래에 둔다.
- 새 도메인은 `app/<domain>/routes.py`에 두고 `main.py`에서 등록한다. 리포지토리는 InMemory + Postgres를 두고 `app.state`로 주입한다.
- 새 테이블은 `systems/order/shared/kis_trader/migrations/00NN_*.sql` 새 파일로만 추가하고 `docs/ERDCLOUD_IMPORT.sql`, `docs/erd/*.sql`, `docs/erd/TABLE_ROLES.md`를 같은 PR에서 갱신한다.
- 오류 응답은 FastAPI `HTTPException` 관례대로 `{ "detail": { "code": "...", ... } }` 형태다. 이 문서의 `{ code: ... }` 표기는 `detail` 안의 내용을 뜻한다.
