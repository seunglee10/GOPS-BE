# GOPS Agent Analysis Query Policy

이 문서는 분석 에이전트가 우선 처리할 쿼리 종류와 목표 답변 형태를 정한다.
구현 기준 문서는 `AGENT_ARCHITECTURE.md`, `AGENT_BACKEND_INTEGRATION.md`,
`AGENT_FRONTEND_INTEGRATION.md`를 따른다.

## Goal

에이전트 답변은 근거 나열이 아니라 결론 중심 분석이어야 한다. 첫 문장은
"무엇을 확인했다"가 아니라 "가장 그럴듯한 해석은 무엇인가"로 시작한다.

기본 답변은 다음 판단을 압축해서 보여준다.

```text
무슨 일이 있었나?
그 일이 원래 기대보다 좋았나/나빴나?
가격은 시장/섹터/관련 종목 대비 다르게 움직였나?
뉴스와 가격 반응의 시간 순서가 맞나?
가장 그럴듯한 원인은 무엇인가?
반대로 볼 점과 다음 확인 포인트는 무엇인가?
```

## Priority Query Types

| Priority | Query type | Example | Target answer |
| --- | --- | --- | --- |
| P0 | 가격 변동 원인 분석 | `왜 올랐어?`, `NVDA 왜 떨어졌어?` | 가격 변동을 뉴스, 시장/섹터, 차트 수급, 관계 종목 신호로 분해하고 가장 가능성 높은 원인을 제시한다. |
| P1 | 뉴스와 가격 반응 불일치 | `뉴스는 좋은데 왜 주가는 빠져?` | 뉴스 headline 감성과 실제 가격 반응이 어긋난 이유를 기대 선반영, 가이던스/숫자 부족, 섹터 약세, 밸류에이션 부담 같은 후보로 좁힌다. |
| P1 | 시장/섹터/개별 요인 분해 | `이거 개별 이슈야 시장 이슈야?` | 종목 움직임이 시장 공통 요인인지, 섹터/테마 요인인지, 개별 뉴스 요인인지 결론을 낸다. |
| P2 | 선택 reference 기반 분석 | `이 뉴스 왜 그래?`, `이 봉 왜 저래?` | `references`, `uiContext`, `chartContext`의 선택 뉴스, 일일 뉴스 요약, 선택 봉, visible candle 구간을 분석 기준점으로 사용한다. |
| P2 | 뉴스 영향 종목/섹터 매핑 | `이 뉴스 영향받는 종목 뭐야?` | Graph expansion, ontology 관계, 관련 뉴스 심볼을 내부 신호로 써서 관련 종목/섹터를 제한된 개수로 제시한다. |
| P4 | 실적 발표 반응 분석 | `실적 발표 왜 이렇게 반응했어?` | v1에서는 낮은 우선순위다. 재무/실적 데이터가 충분할 때만 headline, 숫자, 가이던스, 가격 반응의 불일치를 설명한다. |

P3는 예약 단계다. v1에서는 응답 시간과 데이터 품질이 더 중요한 P0-P2를 먼저
안정화한다.

## Answer Shape

최종 답변은 다음 원칙을 지킨다.

- 첫 문장은 결론 또는 가장 그럴듯한 해석으로 시작한다.
- 본문은 `핵심 판단`, `왜 그렇게 보나`, `반대로 볼 점`, `다음 확인 포인트` 중
  필요한 2-3개만 사용한다.
- 근거 bullet은 최대 3개로 제한한다.
- 뉴스 링크, role별 답변, provider 상태, raw evidence는 기본 본문 뒤의 보조
  정보로 둔다.
- `조회했습니다`, `근거가 있습니다`, `providerEvidence`, `Provider status` 같은
  상태 보고형 표현은 최종 요약에 쓰지 않는다.
- 투자 권유가 아니라 분석 보조 답변으로 작성하고, 직접 매수/매도 명령은 내리지
  않는다.

권장 출력 예시는 다음과 같다.

```text
가장 그럴듯한 해석은 개별 뉴스보다 시장/섹터 압력이 더 컸다는 것입니다.

핵심 판단
- 선택한 뉴스 자체는 긍정적이지만, 같은 시간대 차트 반응은 섹터 약세와 더 잘 맞습니다.
- 관련 종목도 비슷하게 밀렸다면 개별 악재보다는 공통 요인 가능성이 큽니다.

반대로 볼 점
- 뉴스 직후 거래량이 급증했다면 단기 차익 실현이나 기대 선반영도 같이 봐야 합니다.

다음 확인 포인트
- 같은 구간의 지수/섹터 ETF/주요 peer 수익률을 비교하세요.
```

## Data Use

현재 데이터 흐름을 그대로 활용한다.

- `references`는 사용자가 선택한 차트 봉, 차트 구간, 뉴스 기사, 일일 뉴스 요약을
  담는다. 선택 reference가 있으면 일반 종목 요약보다 우선한다.
- `uiContext`는 active panel, selected/hover reference, visible range 같은 화면
  상태 hint로만 사용한다.
- `chartContext`는 visible candles, visible change, selected candle, data status를
  담는다. 가격 반응의 시간 순서와 강도를 판단할 때 사용한다.
- `dailySummaries[].priceChange`는 뉴스 일자와 1D 가격 반응을 연결하는 주요
  입력이다.
- 뉴스 `impactDirection`, relevance, importance는 headline 감성보다 낮은 수준의
  근거로만 쓰고, 최종 판단은 가격 반응과 함께 종합한다.
- GraphDB, ontology, graph expansion은 관련 종목/테마 후보를 찾는 내부 신호다.
  사용자 답변에는 `GraphDB 기준`, `ClickHouse`, `Redis` 같은 저장소명을 노출하지
  않는다.

## Confidence Display

사용자 표시용 신뢰도는 completed report의 `finalResponse.confidence`를 기준으로
한다. 프런트 타입은 이 값을 `finalResponse.confidence?: number`로 보존해야 한다.

최종 답변 하단에는 작은 점으로 신뢰도를 표시한다.

| Dot | Threshold | Tooltip |
| --- | --- | --- |
| 초록 | `confidence >= 0.75` | `신뢰도 75%` 이상 |
| 노랑 | `0.50 <= confidence < 0.75` | `신뢰도 50-74%` |
| 빨강 | `confidence < 0.50` | `신뢰도 49%` 이하 |

마우스 hover 시 실제 값을 반올림한 퍼센트로 표시한다. 예: `신뢰도 78%`.

## Tests And Evaluation

라우팅 테스트는 최소한 다음 문장을 포함한다.

- `NVDA 왜 떨어졌어?`
- `뉴스는 좋은데 왜 주가는 빠져?`
- `이거 개별 이슈야 시장 이슈야?`
- `이 뉴스 영향받는 종목 뭐야?`
- `선택한 봉 왜 저렇게 움직였어?`

합성 테스트는 다음을 확인한다.

- 최종 요약이 결론으로 시작한다.
- 근거 bullet이 과도하게 늘어나지 않는다.
- `GraphDB`, `providerEvidence`, `Provider status`, `ClickHouse`, `Redis` 같은 내부
  표현이 기본 사용자 본문에 나오지 않는다.
- 선택 뉴스/선택 봉이 있으면 해당 reference가 분석 기준점으로 사용된다.

프런트 테스트는 다음을 확인한다.

- 초록/노랑/빨강 threshold가 정확하다.
- hover title 또는 tooltip에 `신뢰도 NN%`가 표시된다.
- 근거/role 답변이 기본 본문보다 먼저 나오거나 본문을 덮지 않는다.

