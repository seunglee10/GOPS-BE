# 01. Bid/Ask 차트: 당일 인트라데이 전환 (프론트엔드)

## 목표

Bid/Ask 차트(`chartType: "bidask"`)를 "여러 날 × 하루 1컬럼"에서 **"오늘 하루 × 인터벌
버킷 1컬럼"** 인트라데이 footprint로 전환한다. 인터벌은 1m/10m/1h, 기본 10m.
과거 세션은 그리지 않으며 daily 데이터 의존을 제거한다.

이 전환으로 기존의 "그래프가 작다" 문제는 구조적으로 해소된다: 가격축이 당일
범위로만 잡히므로 사다리가 패널 높이를 자연스럽게 채운다.

## 현재 동작 (변경 대상)

- bidask는 1D 전용으로 강제된다: `orderFlowActive = chartType==="bidask" &&
  interval==="1D"` (`apps/gops-frontend/src/components/ChartPanel.tsx:335`),
  인터벌 셀렉터 비활성+1D 강제 (`PanelContentRenderer.tsx:272-276, 342`),
  진입 시 `visibleCount = orderFlowDefaultVisibleDays(24)` (`ChartPanel.tsx:224,
  1113-1134`).
- 데이터: 과거 일별 `fetchOrderFlowDaily`(보이는 날짜 범위 키로 재요청,
  `ChartPanel.tsx:881-908`) + 당일 `fetchOrderFlowIntraday` + WS
  `ORDER_FLOW_BINS_UPDATE` (`ChartPanel.tsx:910-943`).
- 렌더: 보이는 1D 캔들 unit마다 그 날짜의 사다리 컬럼을 그림
  (`ChartCanvas.tsx:737-798` `drawOrderFlowColumns`), 하루 스팬이 좁으면 고정 px로
  재포장 (`orderFlowRender.ts:575-605`).

## 변경 사양

### A. 인터벌 모델: 1D → 1m/10m/1h (기본 10m)

- bidask 허용 인터벌을 `["1m","10m","1h"]`로 정의하고 진입 시 `10m`으로 설정한다.
  candle→bidask 전환 시 현재 인터벌이 허용 목록에 있으면 유지, 없으면 10m.
- 인터벌 셀렉터를 bidask에서 활성화하되 허용 3종만 노출한다
  (`PanelContentRenderer.tsx`의 bidask 분기 수정).
- `orderFlowActive` 조건을 `chartType==="bidask" && interval ∈ {1m,10m,1h}`로 변경.

### B. 시간축/슬롯: 해당 인터벌 캔들을 백본으로 재사용

기존 bidask가 1D 캔들 unit을 슬롯 백본으로 쓰는 구조를 그대로 유지하고 인터벌만
바꾼다. 즉 **bidask@10m = 10m 캔들 차트에서 캔들 대신 사다리를 그리는 것**이다.
시간축 라벨·줌·팬·세션 경계는 기존 인트라데이 캔들 로직이 그대로 적용되므로 시간축
코드는 수정하지 않는다.

- 진입 시 보이는 창: **당일 정규장 전체**(10m=39, 1h=7, 1m=390 버킷). 1m은 390슬롯이
  좁으므로 진입 직후 최근 ~120버킷을 보여주고 스크롤로 탐색 가능하게 해도 된다
  (`defaultVisibleBarsForInterval` 기존 값 재사용 가능). 구현 단순성을 위해
  "10m/1h는 세션 전체, 1m은 기존 인트라데이 기본 창"을 채택한다.
- 과거로 스크롤하면 전일 캔들 슬롯이 나오지만 오더플로우 데이터가 없으므로 기존
  ghost candle 폴백(`drawOrderFlowGhostCandle`, `ChartCanvas.tsx:856-862`)이 그려진다.
  이는 허용 동작이다(과거 미지원의 자연스러운 표현).
- 오더플로우 bins는 정규장 체결만 집계되므로(`alfaka/orderflow/bins.py`의 regular
  세션 필터) pre/after 슬롯도 ghost로 나타난다. 허용 동작.

### C. 데이터: intraday 단일 소스, daily 의존 제거

- 유지: `fetchOrderFlowIntraday`(`orderFlowClient.ts:51-58`) 시딩 + WS
  `ORDER_FLOW_BINS_UPDATE` 분 갱신(`replaceOrderFlowMinute`) — 현행 그대로.
- 제거: `orderFlowDaily` 상태, `fetchOrderFlowDaily` 호출과 `visibleOrderFlowRange`
  재요청 로직(`ChartPanel.tsx:268-270, 336-347, 881-908`), `orderFlowClient.ts`의
  daily fetch 함수와 관련 타입 사용처. `GET /api/charts/order-flow/daily`는 백엔드에
  남지만(README 결정: deprecated) 프론트는 호출하지 않는다.
- 버킷 사다리 구성: 분 단위 `orderFlowToday` Map에서 버킷 범위(10m=연속 10개 분)를
  기존 `sumMinuteWindows`(`orderFlow.ts:168-195`)로 합산해 캔들 unit의 버킷 시각과
  매핑한다. 기존 "sessionDate→일별 사다리" 조회(`drawOrderFlowColumns` 내부)를
  "bucketStart→분 윈도우 사다리" 조회로 교체. 계산 결과는 기존 WeakMap 캐시 패턴
  (`ChartCanvas.tsx:47, 811-825`)을 따라 분 데이터 갱신 시에만 재계산한다.

### D. 가격축과 사다리 스팬

- `priceDomain`(`scene.ts:520-548`)은 수정하지 않는다. 보이는 창이 당일 인트라데이
  캔들이므로 도메인이 자동으로 당일 범위가 된다 — 이것이 이번 전환의 핵심 이득.
- 재포장 로직(`packedChartPriceMapper`, `orderFlowRender.ts:575-605`)은 남겨두되,
  버킷 native 스팬이 대부분 minSpan을 넘게 되므로 사실상 native 매핑이 쓰인다.
  10m 기본 뷰에서 재포장이 여전히 빈번하면 그때 minSpan 조정을 검토한다(선조치 금지).

### E. 라이브 갱신

- WS 분 갱신이 도착하면 해당 분이 속한 버킷의 캐시만 무효화한다.
- 진행 중 버킷은 마지막 WS/스냅샷 상태로 그린다(부분 버킷 표시 허용, 별도 마킹 불요).

## 수용 기준

1. bidask 진입 시 10m 인터벌로 당일 정규장 버킷들이 사다리 컬럼으로 표시된다.
2. 인터벌 셀렉터로 1m/10m/1h 전환이 되고, 각 버킷 사다리는 해당 범위 분들의 합과
   일치한다(단위 테스트: 고정 분 데이터로 10m 합산 검증).
3. 가격축이 당일 범위로 잡혀 사다리가 패널 높이 대부분을 사용한다(스크린샷 검수).
4. 네트워크 탭에서 `order-flow/daily` 요청이 더 이상 발생하지 않는다.
5. candle/line/ohlc 차트의 동작은 변하지 않는다.
6. `tsc -b`, `vite build`, `node scripts/run-chart-tests.mjs` 통과.

## 파일 목록

- `apps/gops-frontend/src/components/ChartPanel.tsx` (인터벌 모델, 상태·fetch 정리)
- `apps/gops-frontend/src/components/PanelContentRenderer.tsx` (인터벌 셀렉터)
- `apps/gops-frontend/src/chart/ChartCanvas.tsx` (버킷 사다리 조회로 교체)
- `apps/gops-frontend/src/chart/orderFlow.ts` (버킷 집계 헬퍼 — 기존 함수 재사용 중심)
- `apps/gops-frontend/src/chart/orderFlowClient.ts` (daily fetch 제거)
- `apps/chart-engine/src/*` (bidask 인터벌 허용 목록이 engine capabilities에 걸리면 동기 수정)
- 차트 테스트 스위트에 버킷 합산·인터벌 전환 케이스 추가
