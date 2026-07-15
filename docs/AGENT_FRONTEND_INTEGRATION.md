# GOPS Agent Frontend Integration

이 문서는 새 프런트엔드나 기존 `gops-frontend`가 GOPS 에이전트를 붙일 때
지켜야 하는 계약을 정리한다. 백엔드 route와 delivery semantics는
`AGENT_BACKEND_INTEGRATION.md`를 따른다.

## Frontend Role

프런트는 사용자의 질문, 현재 종목, 차트/레이아웃 context를 백엔드에 보내고
`analysisId` 기준으로 report를 렌더링한다.

프런트가 담당하는 것:

- chat input and message context
- current symbol and watch context
- optional `chartContext`
- optional `layoutContext`
- report polling or SSE subscription
- final answer, evidence, role findings rendering
- optional chart/layout proposal preview and apply flow

토요일 시연에서는 상단 LIVE/SIM 토글과 `다음 시연 단계` 제어가
`/api/simulator/*`를 사용한다. 상태 응답의 `phases`, `phaseIndex`, `nextPhase`를
기준으로 `시장 조망 → 지정학 이벤트 → 장 마감·복기`를 이동하며, 임의의 클라이언트
타이머로 단계를 추정하지 않는다. SIM 전환 직후 AMD와 OKE의 자연스러운 trade/quote가
흐르고 첫 `다음` 입력은 `breaking-event`로 바로 이동한다.

SIM 상태에서 트리맵은 status의 AMD/OKE 가격·등락률을 1초 단위로 반영한다. 기존 시연용
추천·기업·차트 패널은 발표자가 단계 이동과 별개로 사용한다.
뉴스 패널은 이벤트 전 빈 결과를 사용하고 이벤트 뒤 `/api/simulator/news`의 속보를
정상 뉴스 카드로 표시한다. 화면에 보이는 제목·요약·출처에는 시뮬레이션 표기를 넣지
않지만, API의 내부 합성 데이터 식별 정보는 유지한다. IFF 차트 해설은
`GlossaryText`를 통해 차트 해설과 Wild 답변에 같은 투자 용어 설명을 제공한다.
공용 사전은 일반 단어를 주석하지 않고, 처음 보는 투자자에게 필요한 전문용어만
한 문장으로 설명한다. 장 마감 단계의 AI 코치 fixture도
`saturday-demo-close-report`로 표시해 실제 저장 report와 혼동하지 않는다.

지정학 이벤트와 장 마감 알림은 시뮬레이터 전용 toast를 만들지 않고 기존
`AlertToast` 큐, 헤더 알림함, 알림 환경설정을 그대로 사용한다. 지정학 이벤트 toast와
헤더 알림 행에는 기존 위험 색상 토큰을 옅게 섞은 붉은 강조색을 사용한다. 지정학 이벤트
toast는 현재 toast보다 우선 노출해 뉴스 공개와 같은 상태 전환에서 즉시 확인할 수 있다. 이벤트의
`근거 보기`는 보유종목, AMD 차트, 가상계좌, 알림 설정, OKE 오더플로우 패널을 여는
layout proposal이다. 사용자가 버튼을 눌러야 적용하며 주문은 실행하지 않는다. 주문
패널의 바스켓도 사용자가 직접 눌러야 전송하고, SIM 표시가 있는 주문은 실제 브로커
WebSocket에 연결하지 않는다.
시뮬레이터 상태는 실행 중에만 1초 간격으로 확인하고 LIVE, 일시정지, 완료,
연결 불가 상태에서는 30초 간격으로 낮춘다. 이전 요청이 끝난 뒤 다음 요청을
예약하며, 브라우저 탭이 백그라운드에 있으면 polling을 중단하고 다시 보일 때 즉시
한 번 갱신한다.
SIM에서 LIVE로 돌아갈 때 프런트는 합성 캔들·체결·호가가 남은 차트 런타임을
초기화하고 차트 컴포넌트를 다시 연결해 실제 시장 스냅샷과 WebSocket을 새로 받는다.
`PUT /api/simulator/mode`의 LIVE 응답은 서버가 SIM 시작 직전 보관한 AMD/OKE
Redis 시장 상태를 복원한 뒤 반환하므로, 프런트 재연결이 합성 봉을 다시 읽지 않는다.

`빠른 주문` 패널도 자동 주문 경로가 아니다. 최우선 매수·매도호가, 1틱 오프셋,
estimated order-flow imbalance는 `side + price` 주문 의도를 선택해 편집 가능한 가격 입력란을
채우는 프리셋이다. 사용자는 선택한 가격을 전송 전에 직접 수정할 수 있으며,
사용자가 패널 하단의 주문 전송 버튼을 눌러야만 기존 `POST /api/orders`를 호출한다.
`202` 응답은 접수로 표시하고 체결은 `/ws/orders/{order_id}`의 terminal event로
확인한다. 빠른 주문은 호가 이벤트 시각을 연결 상태 판단 기준으로 사용하지 않는다.
Bid/Ask 구조가 유효하지 않거나 chart WebSocket이 연결 오류 상태이거나 order-flow
미지원 종목일 때만 전송을 비활성화한다. 로컬 SIM의 지정가 체결가는 replay engine 기준이며 실제 지정가
matching을 의미하지 않는다.

일반 주문 ticket과 빠른 주문은 전송 전에 compact `AI 코치 판단 기록` picker를
표시한다. 사용자가 실제로 확인한 항목만 선택하며, UI의 여섯 key는 `RSI`
(`chart.rsi`), `MACD` (`chart.macd`), `거래량` (`chart.volume`), `기업 뉴스`
(`news.company`), `실적·재무` (`fundamentals.earnings`), `시장·섹터`
(`market.context`)다. Submit payload는 여섯 항목 모두를 `checked` 또는
`unchecked`로 명시하는 `decision-checks.v1`을 포함한다. Picker는 주문 성공 뒤에만
초기화하며 실패한 요청에서 사용자의 선택을 잃지 않는다. 프런트는 label, evidence,
source, capture timestamp를 보내지 않으며 서버가 검증·보강한 fill event만 AI 코치
판단 근거로 사용한다.

패널 팔레트의 `가상 빠른 주문`, `가상 주문`, `가상계좌`는 기존 레이아웃에 자동
추가하지 않는다. 두 가상 주문 패널은 KIS 주문 컴포넌트의 형태를 재사용하지만
`/api/paper/*`와 `/ws/paper/*`만 호출하며 LIVE/SIM 토글의 영향을 받지 않는다.
가상 빠른 주문은 `/api/paper/symbols/search`의 전체 활성 미국 주식/ETF를 선택할 수 있고 유효한 bid/ask가
없으면 전송을 비활성화한다. 일반 가상 주문은 호가가 없어도 지정가를 대기 주문으로
접수한다. `가상계좌`는 현금, 평가손익, 보유종목, 미체결 취소, 거래내역을 제공한다.
첫 번째 `예약 매매` 탭은 기존 `/api/trade-conditions` 목록·등록·일시정지·알림·삭제
기능을 가상계좌 표 스타일 안에서 제공하며, 조건 충족 주문은 기존 영구 가상계좌
실행 경로를 그대로 사용한다. 가격 조건 화면은 별도 패널에 중복 표시하지 않는다.
기존 `priceCondition` panel type은 저장된 레이아웃 호환을 위해 유지하되 팔레트 제목은
`알림 설정`이고 알림·관심 기업 설정만 표시한다.

Agent 인증 진입은 상단 global navigation의 `Login` 버튼을 사용한다. 별도 `Agents`
버튼은 표시하지 않으며, 인증 후 하단 Agent 입력을 직접 사용한다. 로컬 Vite DEV에서는
Agent debug가 기본으로 켜지고 분석 prompt를 보내면 request snapshot을 browser
console과 `window.__GOPS_AGENT_LAST_REQUEST__`에 기록한다. `?agentDebug=0`은 해당
브라우저의 localStorage에 opt-out을 저장하고, `?agentDebug=1`은 다시 켠다.
production build에는 debug snapshot을 노출하지 않는다.

상단 global navigation은 `SimulatorControl`과 `Login` 사이에 알림 종을 둔다. 종의
뱃지는 `/api/notifications` 및 `/ws/notifications`의 사용자별 안읽음 수를 표시하고,
종 아래에 연결된 알림함에서 개별 `PATCH /api/notifications/{id}/read`와 전체
`PATCH /api/notifications/read-all`을 호출한다. 알림 토스트는 기존 화면 우하단
위치를 유지하며, 헤더에서 읽은 영속 알림은 현재 토스트 대기열에서도 제거한다.

프런트가 담당하지 않는 것:

- provider 직접 호출
- Kafka 직접 produce/consume
- ClickHouse/GraphDB 직접 query
- 사용자 확인 없이 분석 결과만으로 주문을 실행하는 자동화

## 차트 가격 선택과 paper 예약매매 확인

오른쪽 가격축의 가격 pane을 pointer로 선택하면 프런트는
`ChartPriceSelection.v1` 불변 snapshot을 만든다. `PanelWorkspace`는 마지막으로
focus/pointer 선택한 `OrderTicket`·`QuickOrderPanel`(paper 변형 포함), 또는 화면
순서상 첫 주문 패널 하나에만 이를 typed prop으로 전달한다. 주문 패널은 종목과
지정가만 바꾸고 수량·매수/매도 방향을 보존하며 자동 제출하지 않는다.

`이 가격에 예약하자`, `이 때 사자` 같은 차트 맥락 문장은 Agent/주문/알림 API보다
먼저 로컬 확인 intent로 분기한다. 현재 `ChartTradeSetup`의 진입·목표·손절과 asset
identity가 완전하고 대상 `chartDocumentId`가 하나로 정해진 경우에만
`TradeAutomationConfirmationDraft.v1` dialog를 연다. 수량 기본값은 시연용 20주이며
사용자가 확인 전에 양의 정수로 바꿀 수 있다. 확인하면 현재 선택 가격을 발동가와
지정가로 사용하고 `executionEnabled=true`, `alertsEnabled=true`, `validity=GTC`인
조건 하나를 `POST /api/trade-conditions`로 등록한다. 이 경로는 영구 가상계좌의
paper 실행만 사용하며 실계좌 주문을 만들지 않는다. 화면에 함께 보이는 목표가·손절가는
분석 참고값이고 bracket/OCO 또는 별도 목표·손절 알림으로 등록하지 않는다.
setup·symbol·interval·asset identity 변경 또는 원본 차트 삭제 시 열린 draft는
`stale`로 바뀌어 확인할 수 없다. API 실패 시 dialog를 유지해 사용자가 수량을 잃지
않고 재시도할 수 있다.

`예약 매수 해줘`, `예약매수 해달라`, `예약 주문해줘`처럼 실행 의사가 분명하지만
가격 지시어가 없는 문장도 일반 분석으로 보내지 않는 거래 명령이다. 현재 차트에서
사용자가 가격축을 선택한 상태면 그 선택 가격으로 같은 확인 dialog를 열고, 선택
가격이 없거나 다른 차트의 선택이면 `어느 가격에 예약할까요?`라고 안내하며
`POST /api/agents/analyze`를 호출하지 않는다. `예약매매가 뭐야?` 같은 설명 질문과
가격 없는 일반 `매수해줘`는 이 로컬 paper 예약 명령으로 추정하지 않는다.
수량이 명령 중간에 들어가거나 `걸어줘`, `넣어줘`, `설정해줘` 같은 실행 동사를
사용한 예약매수·예약매도 표현도 같은 로컬 확인 경로로 처리한다.
`545달러에 예약 매수해줘`, `$545 예약매수 해줘`, `USD 545에 예약 주문해줘`,
`가격 545로 예약 매수해줘`처럼 프롬프트가 양의 가격을 명시하면 가격축 선택 없이
그 값을 발동가와 지정가로 사용해 확인 dialog를 연다. 프롬프트 가격은 남아 있는
차트 가격 선택보다 우선하며, `20주` 같은 수량 숫자는 가격으로 해석하지 않는다.

차트별 `ChartTradeSetupSnapshot`은 App React state로 올리지 않고 `chartDocumentId`별
메모리 store에 보관한다. store는 가격·drawing ID·asset identity 등을 값으로 비교해
동일 snapshot의 저장과 알림을 생략하며, 확인 dialog가 열린 문서만 변경을 구독한다.
차트 crosshair는 정적 캔들·지표·작도 base canvas와 분리된 overlay canvas에서 rAF로
그려 pointer 이동이 workspace 전체 또는 정적 차트 레이어를 다시 렌더하지 않게 한다.

완료 report에 `tradeConditionProposals[]`가 있으면 답변 하단에 가격·방향·지정가·
수량 누락 여부를 표시할 수 있다. 사용자가 이어서 `이 가격에 예약매매랑 알림
걸어줘`처럼 명시적으로 요청한 경우에만 프런트는 가격을 재구성하지 않고
`analysisId`, `proposalId`, 원문 후속 문장을 `POST /api/trade-conditions/commands`로
보낸다. API가 `clarify`를 반환하면 같은 proposal context를 유지해 수량 같은 누락
필드를 받고, `created`일 때만 가상계좌의 예약 매매 탭을 invalidate/refetch한다. 관련
없는 새 분석이 완료되면 이전 proposal context를 폐기한다.

`이 종목을 관심종목에 추가해줘` 같은 문장은 현재 선택된 추천 종목 또는 차트 종목이
있을 때 결정론적 UI 명령으로 처리한다. 프런트는 `GET /api/charts/watchlist`로 현재
목록을 읽고 종목이 없을 때만 `PUT /api/charts/watchlist`로 전체 목록을 교체한다.
이미 등록된 종목에는 쓰기 요청을 반복하지 않는다. 현재 종목을 정할 수 없으면 임의로
티커를 추론하지 않고 먼저 종목 선택을 요청한다.

## Chart Derived Profile

차트의 candle Volume Profile은 Agent feature pack과 별도 계약이다. `ChartCanvas`가
현재 viewport로 만든 scene과 visible closed-candle 범위가 일치한 뒤에만 프런트가
`targetBins=10`, `scene.scales.minPrice/maxPrice`, `candleCount`를 요청한다. 따라서
활성 MA·Bollinger와 축 padding을 포함한 main price pane 전체가 같은 화면 높이의
10개 슬롯이 된다. pane 높이만 바뀌면 기존 가격 bucket을 다시 투영하고 재조회하지
않는다.

응답은 10개 bucket, 요청 가격 경계, 요청/source candle count가 모두 일치할 때만
표시한다. `dataStatus=partial`은 클라이언트 derived cache에 넣지 않고 숨긴 상태로
500ms와 1500ms 뒤 두 번 재시도한다. 계속 partial이면 다음 scene, range, candle
변경까지 숨긴다. 0-volume bucket은 응답에 유지하지만 Canvas는 막대를 그리지 않아
그 가격 슬롯의 빈 공간을 보존한다.
## AI 투자 코치

AI 투자 코치 패널은 가로 2칸을 최소 너비로 사용하며 세로 길이는 레이아웃에 맞춰
확장할 수 있다.

AI 투자 코치의 알람 생성 UI는 4페이지 `실행·알람 관리`에만 둔다. 1페이지의
매도·관찰 조건은 조건명과 현재값·기준값만 보이는 단일 미리보기로 표시한다.
좌우 화살표로 한 조건씩 전환하고 첫·마지막 항목에서는 해당 화살표를 비활성화한다.
활성 항목을 누르면 API를 호출하지 않은 채 4페이지의 같은 후보로 이동해
focus/highlight한다. 유사 사례의 `그때의 실수`, `오늘과 같은 점`, `오늘과 다른 점`도
한 항목씩 같은 방식으로 전환한다. 현재값, 임계값, 연산자, 판단 사유, 추천 행동 같은 상세는
4페이지에서 표시한다. 추천 후보는 `당일 거래에서 제안` 출처만 표시하고, 출처명은
신호색 왼쪽 rail과 표에 붙은 section band로 행 목록과 한 그룹임을 나타낸다.
가상계좌의 예약 매매 목록과 같은 표에서 첫 줄을 종목·항목·현재값·기호 조건·관리 열로 나눈다.
판단 근거와 추천 행동은 사용자가 행을 선택했을 때만 둘째 줄에 표시한다. 이 줄은
티커 아래에는 작은 빈 여백만 유지하고, 나머지 영역을 `판단 근거 | 근거 내용 |
추천 행동 | 행동 내용` 4열로 나눈다.
가로 2칸 최소 너비에서는 표를 가로 스크롤하지 않고 티커, 제안명, 관리 버튼만
field label 없이 표시하며 현재값과 조건은 생략한다. 이때 출처 그룹의 신호색 rail도
숨긴다. 펼친 판단 근거와 추천 행동은 두 개의 label/value 행으로 표시한다.
여러 행을 동시에 펼칠 수 있고 각 행은 독립적으로 닫는다. 사용자가 지원되는 후보의
`알람 추가`를 눌렀을 때만 `POST /api/alerts`를
호출하며 RSI·거래량·집중도처럼 현재 alert API가 지원하지 않는 후보는 `미지원`으로
남긴다. 저장된 주시 알람은 4페이지에 표시하지 않는다.

1페이지의 여러 당일 체결은 종목명 tab row를 만들지 않고 활성 기업 정보 양옆의
화살표로 전환한다. 현재 거래와 유사 사례도 차트 양옆 화살표로 전환하며 화면에는
carousel 위치 숫자를 반복 표시하지 않는다. 화살표는 별도 좌우 column을 점유하지
않고 콘텐츠 가장자리에 작은 overlay control로 표시하며 비활성 끝점은 숨긴다. 판단
요약은 등급 제목이나 상태색 없이 한 문장으로 크게 표시한다. 확인 항목은 `차트`,
`뉴스`, `재무`, `시장` 순서의 2열 overview로 렌더링하고, 기본 화면에는 분류명, 상태,
최대 두 개 핵심 항목명만 크게 표시한다. 세부 수치·출처·기준시각은 tooltip에 둔다.

2페이지 포트폴리오 탭은 별도 API를 호출하지 않고 받은 report의
`marketDiversification`만 렌더링한다. 현재 섹터 비중과 보유 종목의 시장 연동성은
큰 행으로, 최대 3개의 분산 후보 시장은 가로 rail로 표시한다. 후보는 자동 매수
추천이 아니라 검토 비중 범위이며, 상관 데이터가 없으면 숫자나 일반론적 섹터를
채우지 않고 `시장·섹터 상관 데이터 연결 대기`를 표시한다.

## User Flow

```mermaid
flowchart TD
  User["User question"] --> FE["Frontend Agent input"]
  FE --> Submit["POST /api/agents/analyze"]
  Submit --> Queued["202 queued + analysisId"]
  Queued --> Wait["SSE stream or polling"]
  Wait --> Report["AnalysisReport"]
  Report --> Answer["Wild panel final answer"]
  Report --> Evidence["evidence and role findings"]
  Report --> OptionalUI["optional layout/chart proposal"]
  Report --> PriceProposal["optional price-condition proposal"]
  PriceProposal --> Confirm["explicit user follow-up"]
  Confirm --> ConditionAPI["trade-condition command API"]
```

사용자 입력이 회사명/티커 단독이거나 `애플차트 보여줘`, `AAPL chart` 같은
chart-open 명령이면 프런트는 먼저
`GET /api/agents/entities/resolve?mode=chartShortcut`를 호출한다. 응답이
`chartShortcut=true`이고 `chartAction="replace"`이면 기존 chart symbol 선택
흐름만 적용하고 `/api/agents/analyze`는 호출하지 않는다. 응답이
`symbols`를 2개 이상 포함하면 첫 번째 symbol을 기본 chart에 표시하고 나머지는
`POST /api/agents/layout/resolve`의 chart add proposal로 추가한다. 응답이
`chartAction="add"`이면 차트 화면에서는 `chartAction`, `chartTargetSymbol`,
`chartPlacementIntent`, 현재 `layoutContext`를 보내 기존 chart를 유지한 추가
chart panel proposal을 받는다. 차트 화면이 아니면 첫 번째 chart로 진입한 뒤 추가
panel proposal을 적용한다. `엔비디아 뉴스`,
`엔비디아 분석해줘`, `엔비디아 차트 분석해줘`, 관계 질문, chart registry 미지원
symbol은 기존 분석 흐름을 유지한다.

티커 shortcut이 아니면 프런트는 분석 API를 호출하기 전에
`POST /api/agents/layout/resolve`로 UI-only layout command인지 확인한다.
응답이 `status="ui_layout"`이고 `layoutProposal`이 있으면 즉시 적용하고
사용자에게 `summary`만 표시한다. `status="not_ui"`이거나 route가 실패하면
기존 `/api/agents/analyze` 흐름으로 fallback한다.
`status="ui_clarify"`이면 패널/레이아웃 관련 표현은 맞지만 대상이나 동작이
불명확한 것이므로 `/api/agents/analyze`로 fallback하지 않고 `summary`를 상단
Agent 결과 알림으로 표시한다.

기본 화면 프리셋은 `추천종목`, `기업분석`, `차트분석`, `포트폴리오` 네 개다.
`오늘의 추천 종목 보여줘`처럼 프리셋 이름과 화면 전환 표현이 함께 있는 요청은
기존 `layout.load` fast path로 `추천종목`을 연다. 이전 명칭인 `시장분석`,
`종목분석`, `비교분석`, `자산현황`은 기존 채팅 명령 호환을 위한 alias로만 남긴다.

추천 목록의 종목 클릭은 차트 이동이 아니라 현재 추천 종목 선택이다. 선택값은
`recommendation.stock` reference로 만들어 하단 Agent 입력의 `추천` chip에 표시하고,
추천 당시 순위·점수·신뢰도·근거·위험 경고를 분석 요청의 `references`에 보낸다.
한 번에 하나의 추천 종목 reference만 유지하고 뉴스·차트 reference와는 함께 사용할
수 있다. 선택 행은 뉴스 reference와 같은 주황색으로 강조한다. `이 종목의 기업에 대해 자세히
알려줘`처럼 선택 종목을 가리키는 기업 상세 요청은 분석 API를 호출하지 않는
UI-only fast path다. 선택 종목으로 `기업분석` 프리셋과 회사 패널 symbol을 함께
갱신하고, 성공 시 채팅 답변을 만들지 않는다. 선택값이 없으면 현재 URL의 다른
symbol을 추측해서 쓰지 않고 추천 종목을 먼저 선택하라고 안내한다.

## Submit Request

프런트는 최소한 `symbol`, `intent`, `messages`를 보낸다. 가능한 경우 현재 차트와
레이아웃 context도 같이 보낸다.

```json
{
  "symbol": "NVDA",
  "intent": "NVDA랑 AMD 관계랑 최근 뉴스 같이 분석해줘",
  "routerMode": "hybrid",
  "messages": [
    {"role": "user", "content": "NVDA랑 AMD 관계랑 최근 뉴스 같이 분석해줘"}
  ],
  "chartContext": {
    "symbol": "NVDA",
    "interval": "1m",
    "visibleRange": null
  },
  "layoutContext": {
    "activePanelId": "chart-main",
    "panels": []
  },
  "references": [
    {
      "type": "chart.candle",
      "sourcePanelId": "content-chart-1",
      "displayLabel": "NVDA 1D 2026-07-04T00:00:00Z",
      "data": {
        "symbol": "NVDA",
        "interval": "1D",
        "timestamp": "2026-07-04T00:00:00Z",
        "close": 145.5
      }
    }
  ],
  "uiContext": {
    "activePanelId": "content-chart-1",
    "activePanelType": "chart",
    "selectedReference": null,
    "visibleRange": null
  },
  "requestId": "agent-request-client-id",
  "chartAction": null,
  "chartTargetSymbol": null,
  "chartPlacementIntent": null,
  "mode": null,
  "analysisMode": null,
  "priority": null,
  "responseMode": null
}
```

`references`는 사용자가 명시적으로 선택한 화면 객체를 구조화해 보내는 필드다.
차트 봉, 차트 구간, 뉴스 기사, 일자별 뉴스 요약, 추천 종목, 온톨로지 노드처럼 사용자가
"이거", "여기", "이 뉴스"라고 가리킬 수 있는 객체를 prompt 문자열로 긁어 넣지
말고 별도 reference로 보낸다. `uiContext`는 현재 active panel, visible range,
selection 같은 화면 상태 hint만 담고, provider 조회나 최종 판단은 백엔드/agent가
수행한다.

현재 `gops-frontend`는 canvas chart의 `SemanticSelectionSnapshot`을
`chart.candle` reference로 보내고, news row 선택을 `news.article` 또는
`news.dailySummary` reference로 보내며, 추천 행 선택은 `recommendation.stock`
reference로 보낸다. 사용자가 별도 reference를 선택하지 않아도
`uiContext.selectedReference`/`hoverReference`를 보낼 수 있지만, 명시 선택 chip이나
row selection이 있으면 그것을 우선한다.

Agent submit 시 symbol, interval, `sourcePanelId`, 선택 reference는 하나의 chart panel을
가리켜야 한다. 프런트는 선택 reference를 소유한 panel을 우선하고 해당 handle의
viewport 종료 index까지만 candle을 보낸다. payload에는 viewport 이전 최대 120봉만
pre-roll로 포함하며 viewport 뒤의 candle은 포함하지 않는다. `analysisWindow`와
`assetIdentity`는 서버 재검증을 위한 hint이고 계산 원본을 대체하지 않는다.

캔들·뉴스 선택 overlay의 `ContextualAgentAskButton`은 reference와 각각 기본 문장
`이 봉 분석해줘`, `이 뉴스 설명해줘`를 질문창에 함께 넣는다. 기본 차트분석 layout은 상단 chart `8x4`,
하단 chart commentary `4x2`, news `4x2`이며 저장된 custom layout은 덮어쓰지 않는다.

Bid/Ask chart type or the 오더플로우 panel can send `chart.orderFlow`
references. The reference data should include the selected symbol/session date,
daily or intraday bid/ask totals when available, and
`sideClassification="estimated"` because Alpaca trade aggressor side is inferred
from trades plus top-of-book quotes.

차트 명령의 v1 fast path는 `ChartCommand[]`로 변환되는 영구 변경과
`AgentVisualOverlay[]` 임시 강조를 분리한다. 예를 들어 선택된 봉 기준 수평선은
drawing command로 저장하고, 해당 봉은 canvas에서 만료 시간이 있는 highlight overlay로
잠깐 표시한다.

Drawing anchor는 pixel이 아니라 canonical `timestamp`/`price`를 사용하고
`logicalIndex`는 현재 candle 배열에서 계산 가능한 보조 cache로만 취급한다.
`horizontalLine`은 수동 작도의 단일 anchor와 Geometry 자산의 동일 가격 2-anchor
접촉 구간을 모두 허용한다. 2-anchor 형식의 각 timestamp도 실제 candle key여야 한다.
지원하는 평행선 계약은 2-anchor `horizontalParallelLines`/`verticalParallelLines`, 3-anchor
`trendParallelLines`이며 추세 평행선의 `parallelLineCount`는 2..10이다. 이벤트 설명은
`flagMarker`의 editable label을 사용한다. `rangeBox`와 평행선 band fill은 candle/지표
아래에서, outline·label·selection handle은 chart layer 위에서 렌더링해야 한다.
추세 평행선은 기준선 기준 `0,+1,-1,+2,-2…` 의미 순서로 확장하고 화면 geometry는
공간 순으로 정렬한다. `riskRewardBox`는 `[entry, stop, target]` 세 anchor를 사용하며
target time은 stop time과 같아야 한다. `fibonacciRetracement`는 두 swing anchor와 고정
레벨 `0, 0.236, 0.382, 0.5, 0.618, 0.786, 1`만 사용한다.
모든 fill은 시각 레이어일 뿐 hit-test 대상이 아니며, selection은 line·outline·handle·label로만
수행한다.

Headers:

```text
Idempotency-Key
X-GOPS-User-Id
```

`Idempotency-Key`는 같은 submit을 중복 클릭하거나 네트워크 retry가 발생했을 때
같은 `analysisId`를 재사용하기 위해 필요하다.

`POST /api/agents/layout/resolve`는 idempotency header를 요구하지 않는다. 이 route는
분석 report 생성이 아니라 현재 layout context에 대한 빠른 proposal 판정이다.

## Response Handling

Async response:

```json
{
  "analysisId": "agent-request-id",
  "status": "queued",
  "status_url": "/api/agents/reports/agent-request-id",
  "stream_url": "/api/agents/reports/agent-request-id/stream",
  "report": {}
}
```

프런트는 `analysisId`를 화면 상태의 primary key로 사용한다. `request_id`가 같이
오면 같은 값으로 취급한다.

완료된 idempotent retry나 compatibility mode에서는 `200`과 completed report가
바로 올 수 있다. 이 경우에도 동일하게 `analysisId` 기준으로 렌더링한다.

## Polling And SSE

권장 순서:

1. `stream_url`이 있으면 `GET /api/agents/reports/{analysis_id}/stream`으로 SSE를 연다.
2. SSE가 끊기거나 지원되지 않으면 `GET /api/agents/reports/{analysis_id}` polling으로 degrade한다.
3. report status가 completed, failed, deep_completed, canceled 같은 terminal state가 되면 loading state를 끝낸다.

프런트는 SSE만 전제로 하면 안 된다. Redis pubsub이나 delivery gateway가 준비되지
않은 환경에서는 polling fallback이 필요하다.

## User Cancellation

분석 대기 중에는 채팅 입력은 계속 잠그되 전송 버튼을 중단 버튼으로 바꾼다.
프런트는 submit 전에 client-generated `requestId`를 만들고 request body에
넣어야 한다. 사용자가 중단하면 현재 fetch/SSE/polling을 `AbortController`로
닫고 `POST /api/agents/reports/{analysis_id}/cancel`을 호출한다.

Cancel 후에는 상단 Agent 결과 알림에 중단됨을 표시하고 입력을 다시 연다. 서버가
`canceled` report를 반환하거나 polling/SSE에서 같은 status를 받으면 terminal로
처리한다. 이미 completed/deep_completed/failed가 도착한 뒤의 cancel 응답은 기존
terminal report를 유지할 수 있다.

## Report Rendering

When present, one `coach-report.v2` is passed from the workspace container into the AI
coach panel. The panel has four pages: (1) today's trade review, (2) habit review with
independent `entry`/`exit`/`portfolio` tabs and a six-month point-in-time profile, (3) improvement
priorities, and (4) an alert center for daily-trade recommendations. Page 4 does not
repeat the page header, summary counts, active experiments, enabled guardrails, watched
alerts, or the safety footer. Page sections receive props only and never call the
analysis API.
Page 2 is a long-term investor-profile view, not an alert surface: it renders the supplied
process/outcome cohorts, repeated patterns, and representative trades without a chart or
per-section fetch. If the report has no decision record, it must show the supplied missing
state rather than infer a personality or plan from realized profit and loss.

On page 1, the selected fill and similar-case index are local UI state. A fill switch
selects one `reviewsByFillId` object so chart, missed checks, outcome, portfolio impact,
and conditions change atomically. Price, volume, RSI, and MACD share the `T-60..T+20`
relative axis, and today's path ends at its latest observation without a forecast.
The dev fixture is loaded only by a DEV-only dynamic import when
`VITE_AI_COACH_DEV_FIXTURE=true`; production has no fixture fallback.

When the workspace does not already supply a `coachReport`, the panel makes one top-level
authenticated request to `GET /api/ai-coach/reports/latest`. Child pages never fetch their
own data. A stored report renders immediately; no stored report renders a clear waiting
state. This keeps the post-market coach independent of Redis report delivery while
preserving the existing polling/SSE contract for interactive agent analysis.

Production report의 decision checklist는 post-market input archive에 실제로 있던
기록만 사용한다. Snapshot Builder가 cutoff-safe chart/news/fundamentals/market
evidence를 tooltip과 chart marker에 보강할 수 있지만 그 evidence가
`checked`/`unchecked`를 바꾸지는 않는다. 기록이 없는 체결은 UI가 임의로
`미확인`으로 채우지 않고 `확인 기록 없음`을 표시한다. Historical cases keep their
own decision-check records; a case switch must not reuse the selected current fill's checks.
Decision evidence is bounded by `decisionAt`, while chart outcomes are anchored at
`filledAt` and stop at the report request cutoff.

Portfolio impact renders only an exact fill-scoped `before`/`after` pair. This applies to
both KIS and paper fills; an adjacent account snapshot is not substituted and the UI shows
`계산되지 않음`. Paper cost-basis pairs remain labeled as acquisition-cost data rather
than current market valuation.

Report에서 우선 렌더링할 영역:

- final answer
- agent answers as detailed evidence after final answer
- status and timestamps
- primary symbol
- evidence items
- role findings
- warnings or no-data provider messages
- optional `layoutProposal`
- optional `chartProposal`
- optional `tradeConditionProposals`

Provider가 `status="no-data"` evidence를 반환하는 것은 정상적인 partial analysis다.
예를 들어 GraphDB가 없으면 ontology evidence만 no-data가 되고 market/news 기반
답변은 계속 표시될 수 있다.

`finalAnswer`가 있으면 `agentAnswers`보다 먼저 렌더링해야 한다. 멀티 에이전트
role 답변이 함께 온 경우에도 사용자 화면의 첫 문장은 `finalAnswer.summary`의 종합
판단이어야 하며, role별 답변은 세부 근거로 뒤에 붙인다.

### Wild Panel Answer Pages

현재 `gops-frontend`에서는 workspace 전체에서 한 panel만 Wild가 될 수 있다. Wild
아이콘은 layout edit mode에서만 panel header에 표시한다. 사용자가 다른 panel의 Wild
아이콘을 누르면 기존 Wild panel은 저장된 answer page를 모두 지우고 fixed panel로
돌아가며 새 panel이 Wild destination이 된다. edit mode를 나가면 아이콘만 숨고 Wild
page와 navigation은 유지된다. Wild panel은 원래 content를 base page로 유지한다.
일반 질문의 완료 report는 Wild panel의 다음 page로 자동 추가한다.
`AGENT LOG` button과 drawer는 표시하지 않는다. report 완료 시 Wild panel이 없으면
상단의 3초 결과 알림만 표시하고 상세 report를 나중에 연결하기 위해 보관하지 않는다.
일반 질문을 위해 자동 panel 생성이나 placement picker는 사용하지 않는다.

저장 순서는 base content, `finalAnswer` 기반 최종 답변, role별 상세 답변이다. 새 일반
report는 항상 최종 답변 page를 먼저 연다. chart route에는 범용 snapshot confidence를
정확도처럼 표시하지 않고 quality·패턴 점수·확인 근거를 분리한다.
`investment_advice_limited`, provider/storage 이름, snapshot/LLM fallback 코드는 DOM에
노출하지 않는다. `finalAnswer`가 없으면 일반 summary를 차트 해설로 바꾸지 않고
분석 미완료 상태를 표시한다. 같은 `analysisId`를 같은 panel에 다시 추가하지 않는다.

Agent 동작이 끝나면 top navigation의 center preset dock을 한 줄 결과 알림으로
flip한다. 진행 중 메시지는 표시하지 않고 완료·취소·clarification·실패 결과만
표시한다. 분석 결과는 `MSFT 뉴스를 가져왔습니다.`, `MSFT 차트 분석을 완료했습니다.`
처럼 symbol과 action을 사용한 deterministic 문구이며 3초 뒤 preset dock으로 돌아간다.
알림은 display-only `role="status"`이고 새 결과가 오면 기존 알림을 즉시 교체한다.

Wild state와 answer snapshot은 layout localStorage에 저장하고 panel당 최신 report
10개만 유지한다. fixed state로 되돌리면 별도 확인이나 개별 page 삭제 없이 저장된
Wild page를 모두 제거한다. workspace에 Wild panel이 하나뿐이므로 reload 후에도 해당
panel을 report destination으로 자동 복원한다.
Wild answer payload는 backend `layoutContext`에 넣지 않으며 API/report 계약을 바꾸지
않는다. Wild와 차트 해설은 `finalAnswer`·용어 주석 renderer를 공유한다.

### Chart Commentary Questions

명시적인 차트 질문인 `차트 분석해줘`와 chart/candle reference가 있는 질문은 Wild 대신
요청을 시작한 `chartDocumentId`의 `chartCommentary` panel로 보낸다. panel이 없으면
넓은 화면에서는 원본 차트 오른쪽, 좁은 화면에서는 아래의 빈 grid에 자동 생성하고 기존
Wild panel은 이동·대체하지 않는다. 패널은 현재 Geometry와 로컬 `ChartTradeSetup`에서
즉시 만드는 `현재 해설`, 서버의 요청 시점 snapshot인 `질문 답변`을 분리한다. 요청 중에도
현재 해설을 유지하고 진행 상태만 표시하며, 완료 답변은 문서별 최신 10개를 workspace
layout props에 저장한다.

패널 상단은 연결된 `종목 · 주기`를 표시하고 `차트 선택` 모드에서 대상 차트 외곽선을
강조한다. 다른 차트를 클릭하거나 목록에서 선택하면 `chartDocumentId`를 바꾸며, 각 문서의
답변 기록은 별도로 보존·복원한다. 해설 카드 hover/focus는 transient spotlight, click은
고정 spotlight이며 대상 선은 signal 색상과 증가한 두께로 표시한다.

요청에는 `chartDocumentId`, `sourcePanelId`, 당시 asset identity를 넣는다. 서버 응답의
source document와 현재 문서가 같고 `symbol/interval/assetVersion/algorithmVersion/
inputDigest/asOf`가 모두 일치할 때만 `focusGroups`의 기존 drawing과 로컬 proposal을
transient spotlight한다. 불일치는 `분석 기준 변경됨`, 삭제된 문서는 `원본 차트 없음`으로
표시하고 snapshot 수치는 유지하되 focus하지 않는다. 선택 봉 anchor도 같은 symbol/interval의
canonical timestamp가 현재 candle에 있을 때만 focus한다. 이 상태는 chart history에
저장하지 않는다. 일반 질문은 기존 Wild 흐름을 유지한다.

## Layout And Chart Proposals

에이전트가 `layoutProposal` 또는 `chartProposal`을 반환할 수 있다. 프런트가
이를 지원하면 preview 후 사용자가 적용하도록 만든다.

현재 `gops-frontend`의 active entrypoint는 `App.tsx`이며 `layoutProposal`만
자동 적용한다. `chartProposal`은 차트 엔진 command path와 별도로 연결해야 하며,
현재 App의 일반 분석 렌더링에서는 사용하지 않는다.

`layout.panel.add`가 chart panel을 추가할 때 payload의 `props.symbol`은 새 chart
instance의 symbol이다. `layout.panel.priority.set`과 `layout.panels.arrange`의
`layoutWeight`는 다음 요청의 `layoutContext`에 보존되어, 직전 요청 panel과 chart
panel이 더 크게 배치되고 낮은 priority support panel은 축소/이동될 수 있다.

Rule-based UI parser는 단일 패널 open/close/move/resize 외에도 tidy, close-all,
resize-all, undo, default restore, swap, relative placement, replace, pin/unpin,
save, panel group open을 `layoutProposal.commands`로 반환한다. 프런트는
`layout.undo`용 최근 agent layout 이력을 유지하고, `layout.panel.pin/unpin`을
slot의 `layoutPinned`에 보존하며, `layout.save`는 현재 layout을 custom preset으로
저장한다. `layout.panels.arrange`가 충돌 때문에 일부 생략되거나 readable span으로
정규화되면 적용 결과의 `appliedWithChanges/reason`을 상단 결과 알림에 표시해야 하며 backend
rationale만으로 성공을 단정하면 안 된다.

상단 패널 편집 버튼은 레이아웃 수정모드 토글이다. 비편집 상태에서는 수정모드에
진입하고, 수정 중 다시 누르면 팔레트의 `완료` 버튼과 같은 종료·저장 경로를 사용한다.

UI-only layout 명령이 프런트에 정상 적용되면 symbol-aware 완료 문구를 상단 결과
알림으로 표시한다. `ui_clarify`, `autoApply=false`, undo 이력 없음, placement 선택
필요, 충돌·부분 적용처럼 사용자의 확인이나 조치가 필요한 경우도 같은 알림 영역을
사용하며 placement 후보 적용이 끝나면 완료 문구를 표시한다.

`layoutContext.selectedPanelId`는 "이거", "이 패널", "여기" 같은 지시어의 대상이다.
선택된 패널이 없으면 backend는 임의 패널을 고르지 않고 clarification을 반환한다.
`layoutContext.canUndo`는 "원래대로"가 직전 agent 변경 취소인지 기본 layout
복원인지 결정하는 hint다.

news panel은 `panelType="newsFeed"`와 `props.displayMode="dailySummary"`를
받으면 일자별 요약 전용으로 렌더링한다. 이때 `props.dailySummaries[]`는
`date`, `summary`, `sources[]`를 포함해야 하며, source 항목은 실제 기사
`url`과 링크에 표시할 `title`을 가진다. `priceChange`가 있으면 날짜 옆에
전일 종가 대비 절대 가격 차이를 표시하고, 출처 링크는 요약 아래 `출처` 행의
favicon 아이콘으로 렌더링한다. Redis/ClickHouse 같은 저장소 provenance는 프런트에
표시하는 source 값으로 쓰지 않는다.

market indices panel은 `panelType="marketIndices"`/`kind="indices"`로 표현한다.
현재 `gops-frontend`는 이 패널에서 `GET /api/market/indices`만 호출하며, 차트
panel의 symbol/interval/coverage 상태와 연결하지 않는다. 응답의 `refreshSeconds`,
`cacheStatus`, `warning`, `items[]`를 사용해 자동 새로고침, stale 표시, 행별 가격과
변동률을 렌더링한다.

popular stocks panel은 `panelType="popularStocks"`/`kind="popular"`로 표현한다.
기본 배치와 읽기 가능한 최소 너비는 1 column이며 기본 높이는 2 rows다.
현재 `gops-frontend`는 App이 이미 폴링 중인 `GET /api/market/heatmap?universe=sp500`
items를 패널로 전달해 S&P500 거래대금순 Top10을 렌더링한다. 별도 heatmap 요청은
추가하지 않고, 금액 표시는 `GET /api/market/indices`의 `KRW=X` 환율을 사용해
조원/억원 단위로 환산한다. Top10의 섹터 컬럼은 GraphDB `gops:sector` canonical
값의 `sectorLabelKo` 한글 라벨을 표시하며, 산업명과 섞어 표시하지 않는다.
히트맵/트리맵도 grouping key는 canonical `sector`를 유지하고, 섹터 타일과 hover
표시는 같은 `sectorLabelKo` 한글 라벨을 사용한다.

stock recommendations panel은 `panelType="stockRecommendations"`/`kind="recommendations"`로
표현한다. 패널은 `GET /api/recommendations/stocks/latest`로 마지막 장중 추천을
읽고, 새로고침 버튼은 `POST /api/recommendations/stocks/refresh`에 현재 active
symbol을 보낸다. 패널 상단 왼쪽의 `추천 설정` 버튼은 hover/focus 때 표시되고 중앙
dialog에서 `GET /api/recommendations/profile`로 현재 값을 읽어
`PUT /api/recommendations/profile`로 저장한다. 저장 성공 시 dialog를 닫고 현재
장전/본장 모드의 추천을 다시 조회한다. 추천 행 클릭은 차트 symbol을 바꾸지 않고
`recommendation.stock` Agent reference를 선택하며 주문 실행으로 연결하지 않는다.
추천 행의 섹터도
`sectorLabelKo` 한글 라벨을 사용한다.

chart analysis asset 운영 패널은 `kind="chartAssetOps"`, 화면 표시는
`작도 자산(개발)`로 표현한다. 이름의 `(개발)`은 수동 운영 도구임을 나타내는 라벨일
뿐 표시 게이트가 아니다. 로컬 Vite, Docker production build, 실제 배포 환경 모두
레이아웃 수정 모드의 패널 추가 팔레트에 항상 노출하며 URL query나 localStorage로
숨기지 않는다.

패턴 보유 종목 조회 패널은 `kind="chartPatternList"`, 화면 표시는 `패턴 종목`으로
표현한다. 이 패널은 운영 패널과 분리하며 기존
`GET /api/charts/analysis-assets/coverage` 응답의 `primaryPattern`만 사용한다.
`forming`/`confirmed` 대표 패턴만 종목별로 묶고, 검색과 interval·상태·패턴 종류
필터를 클라이언트에서 적용한다. 빌드 완료·삭제 invalidation event에는 목록을 다시
조회하지만 별도 polling은 하지 않는다. 패턴 항목을 누르면 첫 번째 chart panel의
symbol과 timeframe을 해당 자산 값으로 함께 바꾸며 주문 route는 호출하지 않는다.

Geometry asset은 GET/build/poll route를 사용한다. timed anchor는 현재 interval의
canonical candle timestamp로만 snap하며 대응 봉이 없으면 해당 drawing을 제외한다.
단, Geometry 지지·저항 `horizontalLine`은 가격 자체가 핵심인 무한 수평선이므로
저장된 과거 접촉 봉이 아직 차트에 로드되지 않았으면 현재 로드된 첫·마지막 canonical
candle timestamp에 presentation anchor를 투영해 즉시 표시한다. PostgreSQL 원본
접촉 timestamp는 변경하지 않으며 패턴 경계의 timed anchor에는 이 예외를 적용하지 않는다.
패널은 `1m/5m/10m/1h/4h/1D/1W`를 지원하고 지지·저항, 삼각형·깃발형·페넌트·
직사각형·쐐기·채널 이탈 패턴, coverage,
SMA60·SMA120과 최근 교차 상태를 표시한다. `chart-asset:` 근거와 `chart-plan:` 제안은
차트별 `작도`, `제안` 토글로 독립 제어하며 사용자 수동 drawing은 보존한다. 지지·저항은
기존 zone metadata를 다시 계산하지 않고 2.5px 단일 H-Line으로 표시한다. 패턴 경계는
3.5px 실선이며 대표 경계에 이름·상태를 표시하고 forming은 낮은 불투명도로 표현한다. 새 자산은
`primaryPattern`을 우선 표시하고 기존 geometry 자산은 `primaryTriangle`로 호환한다.
기존 7개 interval 자산은 계속 표시할 수 있지만 새 빌드 선택지는 `1m/1D` 두 개뿐이며
둘 다 기본 선택한다. 동일 실행 중 요청에 합쳐진 경우 이를 안내하고 polling은 기존
job URL을 사용한다. 상태 화면은 수동 우선 작업과 정기 작업을 구분해 표시한다.
완전한 서버 `tradePlan`이 있으면 우선해 `buy_candidate`를 `매수 후보`, `sell_candidate`를
`매도 후보`로 표시하고 `[entry, stop, target]` 순서의 `riskRewardBox`와 세 가격 pill을
함께 적용한다. 서버 플랜이 없으면 현재 또는 가까운 저장 주기의 패턴·지지·저항만으로
조건부 매수/매도 setup을 만든다. 종목별 분기, ATR 재계산, 레벨 재병합, 가짜 candle은
허용하지 않는다. 손익비가 기준 미만인 `no_trade`와 미확정 `watch`는 서버 확정 플랜을
만들지 않는다.
박스의 Entry는 실제 확인 봉 timestamp를 사용하고 Stop/Target의 미래 끝점은 자산에
저장하지 않는 logical index 투영만 사용해 가짜 candle timestamp를 만들지 않는다.
진입 점선은 확인 봉부터, fill과 목표·손절 경계는 마지막 완료 봉 다음 슬롯부터 시작한다.
제안이 보이는 동안 세 가격을 Y축 자동 범위에 포함한다. `DrawingStyle.labelPlacement`와
`zoneSplit`은 command add/update/undo/redo에서 보존하며 값이 없으면 기존 수동 drawing의
inline·axis label과 risk/reward geometry를 유지한다.

완전한 신규 매수 후보만 `chartDocumentId`별 비영속 `ActiveTradePlan`으로 projection한다.
매도 후보와 조건부 setup은 주문 원본으로 오해되지 않도록 `ChartTradeSetup`에만 둔다.
registry는 해당 문서의 심볼·주기 변경, 자산 제거, unmount에서만 clear하고 제안 레이어
숨김에는 유지한다. `gops:trade-plan-updated` detail은 `{ chartDocumentId, plan }`이며
clear에는 `plan:null`을 사용한다. primary chart 해설은 같은 document ID의 projection으로
근거→매수/매도 기준→목표→손절/무효화→action·손익비 단계를 만들고 카드 hover/focus와
click spotlight 동안 해당 문서만 강조한다. 수동 drawing 선 두께는 1~5 범위에서 0.5
간격 slider로 편집하며 command normalizer와 undo/redo도 같은 범위를 사용한다. 이 표시는
교육용 UI이며 주문·알림 route를 호출하거나 신뢰
원본으로 사용하지 않는다.

이 해설·질문 통합은 저장된 Geometry asset의 consumer 변경이다. 배포 시 기존
`geometry_assets`를 그대로 읽으며 chart asset build/FORCE 재생성/migration Job을 실행하거나
Geometry CronJob을 중지하지 않는다. AWS 개발 환경에는
`CHART_INTERPRETATION_ONLY=true FORCE_SERVICES=frontend,agent-orchestrator` 경로로
배포해 frontend, analysis worker, compatibility orchestrator만 교체한다. 공유 agent
image를 사용하는 builder·CronJob·다른 agent workload에는 새 태그를 적용하지 않는다.

SMA 기간은 일수가 아니라 현재 interval의 완료 봉 개수다. SMA60과 SMA120 overlay는
Geometry 자산 적용 시 함께 활성화한다. 골든·데드크로스 metadata의
`cross.timestamp`는 교차 확인 봉, `previousTimestamp`와 `fraction`은 실제 보간 x 좌표,
`cross.price`는 두 이동평균선의 보간 y 좌표이며 프런트가 종가나 현재 viewport 데이터로
대체하지 않는다. 빌드 완료와 삭제는
cache invalidation event를 발생시켜 같은 symbol의 열린
chart/panel을 즉시 재조회한다. 다른 interval의 자산은 적용하지 않는다.

stale 자산은 차트에서 제거하지 않고 낮은 불투명도와 stale badge로 표시한다. 빌드
상태, log, repair 집계는 PostgreSQL polling 응답을 사용하며 SSE와 Redis pub/sub은
사용하지 않는다.

지원하지 않는 경우 정책:

```text
text analysis flow는 유지한다.
layoutProposal/chartProposal은 명시적으로 ignore한다.
ignore한 proposal 때문에 report 자체를 실패 처리하지 않는다.
```

차트 조작은 프런트/차트 엔진의 command contract를 따라야 하며, 에이전트
provider가 직접 UI panel command를 만들면 안 된다.

## Responsive Workspace Layout

`gops-frontend`의 workspace 좌표 계약은 `8 cols x 6 rows`로 유지한다. 화면 폭에
따라 열/행 개수를 연속적으로 바꾸지 않는다. 저장 preset과 agent
`layoutContext.grid`, `placement.col/row/colSpan/rowSpan`이 같은 좌표계를 공유하기
때문이다.

화면 대응은 좌표 개수 변경이 아니라 다음 세 단계 presentation mode로 처리한다.

```text
wide     충분한 셀 크기, 기존 배치 그대로 사용
standard 읽기 가능한 셀 크기, 기존 배치와 panel container query 사용
compact  셀을 더 축소하지 않고 최소 160x110 rendered px를 보장하며 workspace scroll 허용
```

`panelRegistry.ts`는 저장 계약용 `minSpan`과 실제 UI 가독성용
`readableMinSpan`/`minSizePx`를 분리한다. 새 패널 추가, 직접 resize, agent layout
proposal 적용에는 `readableMinSpan`을 사용한다. 예전 저장 layout은 기존 좌표를
복원할 수 있어야 하므로 `minSpan`은 하위 호환 검증 경계로 남긴다.

각 panel frame은 size container다. 좁거나 낮은 패널은 container query로 제목과
padding을 줄이고, non-chart panel body는 필요한 경우 내부 scroll로 degrade한다.
전체 app의 `uiScale`은 layout metric에 전달되어 rendered pixel minimum과 logical
workspace 좌표를 변환한다.

## Alert WebSocket

`WS /ws/agent-alerts`는 notification publisher가 Redis에 publish한 alert를
프런트에 전달하는 bridge다.

active App의 `BottomCommandBar`는 persisted notification용 `WS /ws/notifications`와
broadcast agent alert용 `WS /ws/agent-alerts`를 함께 구독하고 하나의 toast queue로
정규화한다. `NotificationPreferencesProvider`가 `/api/notification-preferences`에서
읽은 전체/유형별/기업별 설정을 새 toast와 이미 대기 중인 toast에 모두 적용한다.
설정을 꺼도 알림 이력과 unread count는 그대로 유지된다.

프런트는 alert를 다음처럼 취급한다.

- market-event explanation 또는 notification decision으로 표시한다.
- 주문 실행으로 자동 연결하지 않는다.
- 사용자가 보고 확인할 수 있는 UI action으로만 이어간다.

가격조건 패널의 `알림` 탭은 리마인더 설정과 `/api/alerts` 기업 조건을 함께 표시한다.
기업 조건 행은 `/api/alerts?includeTerminal=false`가 source of truth이고 기업명을
누르면 조건과 생성 위치를 펼친다. 종은 active/disabled, 휴지통은 삭제를 조작한다.
1회 조건은 발화 후 목록에서 사라진다. 브라우저 event는 refetch invalidation에만 쓴다.

Agent 입력은 일반 분석 전에 `/api/alerts/commands` fast path를 호출한다. `created`는
알림 목록을 갱신하고, `clarify`는 다음 입력에 clarification id를 재사용하며,
`not_matched`만 기존 차트·분석 흐름으로 넘긴다. 조건을 만든 시점에는 toast를 띄우지
않고 `/ws/notifications`의 실제 발화와 재접속 snapshot만 toast queue에 넣는다.

## Frontend Reference Files

기존 구현을 참고할 때 볼 파일:

```text
apps/gops-frontend/src/App.tsx
apps/gops-frontend/src/agent/agentAnalysisClient.ts
apps/gops-frontend/src/agents/agentAnalysis.ts
apps/gops-frontend/src/components/BottomCommandBar.tsx
apps/gops-frontend/src/components/IndexPanel.tsx
apps/gops-frontend/src/layout/agentLayoutTypes.ts
apps/gops-frontend/src/layout/panelLayout.ts
apps/gops-frontend/src/layout/tiledAgentLayout.ts
apps/gops-frontend/src/market/indicesApi.ts
apps/chart-engine/src/agentReference.ts
apps/chart-engine/src/agentChat.ts
apps/chart-engine/src/proposals.ts
apps/chart-engine/src/types.ts
```

새 프런트는 위 구현을 그대로 가져올 필요는 없다. 보존해야 하는 것은 request
shape, `analysisId` 처리, report delivery, proposal ignore/apply 정책이다.

## Frontend Acceptance

```text
사용자가 종목 질문을 보낸다.
회사명/티커 또는 chart-open 명령을 입력하면 entity resolve shortcut으로 차트 symbol만 바뀐다.
분석 요청이면 POST /api/agents/analyze가 호출된다.
queued response의 analysisId가 화면 상태에 저장된다.
SSE 또는 polling으로 completed report를 받는다.
사용자가 중단하면 cancel route가 호출되고 canceled 상태로 loading state가 끝난다.
final answer와 evidence가 렌더링된다.
no-data provider message가 전체 실패로 처리되지 않는다.
layout/chart proposal 미지원 환경에서도 text analysis는 깨지지 않는다.
```
