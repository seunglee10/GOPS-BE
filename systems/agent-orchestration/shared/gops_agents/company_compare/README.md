# company_compare

기업 성향 비교 패널 전담 모듈이다. 구현 기준은 판정이나 점수가 아니라 저장된 근거를
정량 레이어와 서술 레이어로 분리하는 것이다.

## M1-M5 구성

- `agent.py` — 심볼 검증, provider 조회, `company-compare.v1` 응답 조립
- `context.py` — SEC/Yahoo 정량 4개 섹션과 10-K/GraphDB/news 정성 4개 섹션 조립
- `schemas.py` — 활성 섹션을 정확히 한 번씩 요구하는 판정 없는 strict JSON schema
- `synthesizer.py` — 정량·정성 컨텍스트 전용 OpenAI structured output, 금지 표현/근거 참조 후검증
- `cache.py` — 데이터 revision을 포함하는 Redis lazy narrative cache와 메모리 테스트 구현

M1 정량 빌더는 OpenAI를 호출하지 않는다. `ClickHouseFinancialProvider`가 Redis/ClickHouse에
저장된 SEC summary와 peer frames를 읽고, backend fundamentals adapter가 저장된 Yahoo
earnings estimates를 읽는다. 후보 기업은 `GraphDBOntologyProvider`의 same-theme member
evidence에서 만든다. 외부 SEC/Yahoo API를 요청 hot path에서 호출하지 않는다.

M2 서술은 backend가 정량 응답을 만든 뒤 `agent-orchestrator`의
`POST /company-compare/narrative`로 위임한다. orchestrator만 OpenAI Responses API를
strict JSON schema로 호출하며 배포 환경의 `OPENAI_API_KEY`는 AWS Secrets Manager
`/gops/prod/agent-orchestrator/openai/api-key`에서 동기화된다. OpenAI 실패나 키 미설정은
정량 응답을 실패시키지 않고 `narrative.status="failed"`로 degrade한다.

M3는 `TenKProfileProvider`가 Redis `profile:10k:<SYMBOL>` 카드만 읽고,
`GraphDBOntologyProvider`와 `ClickHouseNewsProvider`의 저장 근거를 결합한다. 요청 hot
path는 SEC 문서나 OpenAI 프로파일 생성을 실행하지 않는다. 데이터 레이어는
`business_model`, `growth_style`, `profit_structure`, `financial_health`,
`earnings_stability`, `risk_profile`, `relationship`, `recent_flow` 8개 섹션으로 구성된다.
재료가 없는 값은 생성하지 않고 해당 섹션 또는 값의 `dataGaps`로 남긴다.

M4는 동일 비교의 서술만 lazy cache한다. key는 정렬된 비교 심볼, 질문, 재무·실적
`asOf`, 10-K accession, 뉴스 id/기준시각, 안정된 관계 근거 digest를 포함한다. 기본 TTL은
24시간이며 cache hit도 현재 strict schema, 활성 섹션, evidence ref, 금칙어 검증을 다시
통과해야 한다. 비교 key에는 항상 TTL이 있고 공유 Redis의 report/session/alert 상태를
보호하기 위해 전역 eviction policy는 `volatile-lru`를 유지한다. 인증 사용 환경에서는
기존 사용자별 agent rate limit을 두 비교 POST route에도 적용한다.

M5 프런트는 기본 `기업분석` 프리셋의 첫 8×4 영역을 비교 패널로 사용한다. 구버전 기본
프리셋 저장본에 `companyCompare`가 없을 때만 새 배치로 이행하고 사용자 custom preset은
변경하지 않는다. 화면은 `01—04` 정량, `05—08` 공시·관계, AI 근거 해석 순서이며 긴
10-K 위험 목록과 8개 해석은 펼침 영역으로 정리한다. 성장 차트는 극단값이 다른 지표를
누르지 않도록 지표 행별 최대 절대값으로 막대 길이만 정규화하고 실제 표시값은 서버 값을
그대로 사용한다.

## 응답 경계

```json
{
  "version": "company-compare.v1",
  "status": "ready",
  "baseSymbol": "NVDA",
  "compareSymbols": ["AMD"],
  "quantitative": {
    "sections": [],
    "growthChart": {},
    "alignedFacts": [],
    "dataGaps": []
  },
  "qualitative": {
    "sections": [],
    "dataGaps": []
  },
  "narrative": {
    "status": "ready",
    "summary": "...",
    "sections": [],
    "insights": [],
    "dataGaps": []
  },
  "sources": [],
  "dataGaps": []
}
```

정량 지표에는 `better`, 점수, 추천, `verdict`를 두지 않는다. 결측이나 provider 장애는
값을 만들지 않고 `dataGaps`와 `status="partial"`로 노출한다.

## API

- `POST /api/llm/company-compare` — `{baseSymbol, compareSymbols[], question?}`
- `POST /api/llm/company-compare/quantitative` — LLM을 기다리지 않는 정량·정성 저장 근거
- `GET /api/llm/company-compare/candidates?symbol=NVDA` — 온톨로지 same-theme 후보
- `POST /company-compare/narrative` — backend 전용 agent-orchestrator 내부 route

## M5 품질 확인과 데모

골든셋은 `tests/fixtures/company_compare_golden.json`의 NVDA/AMD, AAPL/MSFT,
JPM/BAC 세 비교쌍이다. 8개 섹션 정확 일치, 비어 있지 않은 해석, 섹션별 근거 인용,
금칙어 전체를 `test_company_compare.py`가 검증한다.

```sh
python3 -m unittest systems.agent-orchestration.tests.test_company_compare
npm --prefix apps/gops-frontend run build
npm --prefix apps/gops-frontend run test:chart
npm --prefix apps/gops-frontend run test:layout
```

로컬 시연은 `http://localhost:5173/?symbol=NVDA`에서 `기업분석` 프리셋을 누른 뒤
온톨로지 후보 `AMD`를 선택한다. 즉시 레이어의 정량 4축과 정성 4축이 먼저 보이고,
같은 데이터 revision으로 이미 생성된 서술은 `검증된 캐시 응답` 배지와 함께 표시된다.
AMD를 제거한 뒤 다시 선택하면 같은 요청의 cache-hit 경로를 반복 확인할 수 있다.
