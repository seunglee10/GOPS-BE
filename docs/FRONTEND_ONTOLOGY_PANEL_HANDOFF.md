# 온톨로지 기업관계 패널 프론트 연동 요청서

## 목적

이 문서는 프론트 담당자가 `기업 관계` 패널만 독립적으로 이식/연동할 수 있도록 정리한 요청서입니다.

핵심은 전체 멀티에이전트 구조가 아니라, **백엔드 분석 결과에 포함된 ontology evidence를 프론트에서 node/edge 그래프로 변환해 렌더링하는 것**입니다.

NVDA는 예시일 뿐입니다. 이 패널은 엔비디아 전용이 아니라, completed analysis report의 `providerEvidence`에 포함된 ontology 관계를 `ticker`, `theme`, `company` 노드와 edge로 변환해 그립니다.

## 중요한 전제

프론트는 `nasdaq-fibo-graphdb-deploy.zip`, GraphDB, SPARQL을 직접 읽지 않습니다.

프론트가 사용하는 입력은 오직 백엔드가 내려주는 completed analysis report의 `providerEvidence`입니다.

정확한 표현은 다음과 같습니다.

```md
GraphDB zip에 존재하는 모든 관계를 프론트가 직접 읽거나 전체 표시하지 않습니다.
프론트는 백엔드가 AnalysisReport.providerEvidence로 내려준 ontology evidence만 렌더링합니다.
새 relationType을 화면에 표시하려면 백엔드가 해당 relationType을 evidence로 내려주고,
프론트 graph mapper가 그 relationType을 처리해야 합니다.
```

## 온톨로지 관련 백엔드 코드 읽기 순서

온톨로지를 사용하려면 백엔드에서는 `사용자 질의 -> ontology role 선택 -> GraphDB 조회 -> EvidenceItem 생성 -> AnalysisReport.providerEvidence` 흐름을 읽으면 됩니다.

```txt
사용자 질의
  -> routing.py에서 ontology role 선택
  -> GraphDBOntologyProvider.fetch()
  -> SPARQL query
  -> row_to_ontology_evidence()
  -> EvidenceItem(provider="ontology")
  -> AnalysisReport.providerEvidence
  -> 프론트 buildOntologyGraphFromEvidence()
```

### 1. 온톨로지 결과가 담기는 report 계약

```txt
systems/agent-orchestration/shared/gops_agents/contracts/__init__.py
```

읽을 코드:

- `EvidenceItem`
- `AnalysisReport.providerEvidence`
- `AnalysisReport.to_dict()`

이 파일은 백엔드가 프론트로 내려주는 최종 report shape를 정의합니다. 온톨로지 결과는 `EvidenceItem(provider="ontology", status="available", raw={...})` 형태로 만들어지고, 최종적으로 `AnalysisReport.providerEvidence` 배열에 담겨 프론트로 전달됩니다.

### 2. GraphDB에서 ontology evidence를 만드는 provider

```txt
systems/agent-orchestration/shared/gops_agents/providers/__init__.py
```

읽을 코드:

- `ProviderRequest`
- `GraphDBOntologyProvider.fetch(...)`
- `row_to_ontology_evidence(...)`
- `ontology_relation_type(...)`
- `themes_by_company_query(...)`
- `control_relationships_by_company_query(...)`
- `companies_by_theme_query(...)`
- `theme_control_relationships_query(...)`

이 파일은 GraphDB/SPARQL 조회 결과를 프론트가 사용할 수 있는 ontology evidence로 바꾸는 핵심 코드입니다. `GraphDBOntologyProvider.fetch(...)`가 ticker와 intent를 받아 theme/control/related company 관계를 조회하고, `row_to_ontology_evidence(...)`가 각 row를 `EvidenceItem`으로 변환합니다.

`ontology_relation_type(...)`은 백엔드 row type을 프론트 graph mapper가 이해하는 `raw.relationType`으로 정규화합니다.

```txt
ticker-theme                  -> theme
ticker-control-relationship   -> control
theme-company                 -> theme-company
theme-control-relationship    -> theme-control
```

### 3. 사용자 질의가 ontology role로 라우팅되는 코드

```txt
systems/agent-orchestration/shared/gops_agents/orchestration/routing.py
```

읽을 코드:

- `KEYWORD_ROUTES`
- `route_intent(...)`

이 파일은 `연관기업`, `관계`, `온톨로지`, `공급망`, `경쟁사`, `섹터` 같은 질의를 ontology role로 보내는 라우팅 규칙을 갖고 있습니다. 예를 들어 `엔비디아의 연관기업 알려줘` 같은 질의가 들어오면 `route_intent(...)`가 ontology 역할을 선택하고, 이후 ontology provider가 GraphDB evidence를 만들 수 있습니다.

### 4. provider evidence가 최종 AnalysisReport로 조립되는 코드

```txt
systems/agent-orchestration/shared/gops_agents/orchestration/workflow.py
```

읽을 코드:

- provider evidence를 orchestration context에 넣는 흐름
- `AnalysisReport(...)`를 생성하면서 `providerEvidence`를 채우는 흐름

이 파일은 provider들이 만든 evidence를 orchestration context에 모으고, 최종 `AnalysisReport`로 조립합니다. 프론트가 받는 `report.providerEvidence`가 어느 단계에서 채워지는지 확인할 때 읽으면 됩니다.

## 프론트가 처리해야 하는 Evidence 조건

그래프로 변환할 evidence는 아래 조건을 만족해야 합니다.

```ts
item.provider === "ontology"
item.status === "available"
```

그 외 provider나 `no-data` evidence는 그래프 변환에서 무시합니다.

지원해야 하는 `raw.relationType`은 다음입니다.

```ts
"theme"
"theme-company"
"control"
"theme-control"
"shared-theme"
"cross-control"
```

## Evidence 예시

아래는 NVDA 예시입니다. 실제로는 AMD, AAPL, MSFT 등 GraphDB에 있고 백엔드가 evidence로 내려주는 다른 ticker도 같은 방식으로 처리됩니다.

```json
{
  "provider": "ontology",
  "status": "available",
  "title": "AI/반도체/데이터센터 관련 기업",
  "summary": "AMD는 AI/반도체/데이터센터 테마에 포함된 기업입니다.",
  "raw": {
    "relationType": "theme-company",
    "ticker": "AMD",
    "companyName": "Advanced Micro Devices",
    "themeName": "AI/반도체/데이터센터"
  }
}
```

## 그래프 변환 규칙

프론트는 `providerEvidence` 배열을 순회하면서 아래 규칙으로 노드와 엣지를 만듭니다.

```ts
theme, theme-company:
  symbol/ticker node -> theme node

control, theme-control:
  symbol/ticker node -> controlled company node

shared-theme:
  each symbols[] node -> theme node

cross-control:
  controllerTicker node -> controlledTicker node
```

예시:

```mermaid
flowchart LR
  NVDA["NVDA"]
  AMD["AMD"]
  ADI["ADI"]
  THEME["AI/반도체/데이터센터"]

  NVDA --> THEME
  AMD --> THEME
  ADI --> THEME
```

## 프론트 구현 참고

현재 참고할 핵심 파일은 다음입니다.

```txt
apps/gops-frontend/src/agents/ontologyGraph.ts
apps/gops-frontend/src/components/OntologyForceGraph.tsx
apps/gops-frontend/src/components/SystemArea.tsx
```

프론트 개편 후에도 유지해야 하는 최소 흐름은 아래입니다.

```txt
completed AnalysisReport
  -> report.providerEvidence
  -> buildOntologyGraphFromEvidence(providerEvidence, symbol)
  -> ontologyGraph panel props
  -> OntologyForceGraph render
```

## 성공 기준

아래 조건을 만족하면 연동 성공으로 봅니다.

- ontology evidence가 없으면 `관계 분석 결과가 아직 없습니다` 상태를 유지한다.
- `theme-company` evidence가 있으면 패널이 비어 있으면 안 된다.
- `NVDA`, `AMD`, `ADI`, `AMAT`, `ANET` 같은 symbol node가 표시될 수 있어야 한다.
- `AI/반도체/데이터센터` 같은 theme node가 표시되어야 한다.
- `theme-company`는 기존 `theme`과 동일하게 `ticker -> themeName` 관계로 그려야 한다.
- NVDA 전용으로 하드코딩하지 않는다.
- 백엔드가 다른 ticker의 ontology evidence를 내려주면 같은 그래프 로직으로 표시되어야 한다.

## 프론트 담당자에게 전달할 핵심 한 줄

이 패널은 GraphDB zip을 직접 읽는 패널이 아니라, 백엔드 completed analysis report의 `providerEvidence` 중 `provider="ontology"`인 항목을 node/edge graph로 변환해 보여주는 패널입니다.
