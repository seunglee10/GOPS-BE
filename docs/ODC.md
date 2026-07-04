# Ontology Data Contract

## 현재 전제

이 문서는 현재 온톨로지 패널이 읽는 리포트 형태를 정리한 보고서다. 프론트는 GraphDB, SPARQL, zip 파일을 직접 다루지 않는다.

온톨로지 패널은 백엔드가 완성한 `AnalysisReport.providerEvidence` 중 ontology evidence만 사용한다. 리포트 API가 달라질 경우 `apps/gops-frontend/src/ontology/ontologyReportClient.ts`와 `apps/gops-frontend/src/ontology/buildOntologyGraphFromEvidence.ts`를 함께 조정하면 된다.

현재 기본 경로는 `VITE_ONTOLOGY_REPORT_URL` 값이며, 값이 없으면 `/api/agents/analyze`를 사용한다. queued 응답이면 `/api/agents/reports/{analysisId}`를 polling해서 완료된 report를 읽는다.

## 리포트 형태

```ts
type AgentEvidenceItem = {
  provider: string;
  status: string;
  title?: string;
  summary?: string;
  url?: string;
  observedAt?: string;
  raw?: Record<string, unknown>;
};

type AnalysisReport = {
  providerEvidence?: AgentEvidenceItem[];
  symbol?: string;
  generatedAt?: string;
};
```

프론트는 `provider === "ontology"`이고 `status === "available"`인 항목만 그래프로 변환한다. 그 외 항목은 무시한다.

온톨로지 evidence가 없으면 패널은 `관계 분석 결과가 아직 없습니다`를 표시한다.

## relationType 처리

| relationType | 필요한 raw 필드 | 그래프 변환 |
| --- | --- | --- |
| `theme` | `ticker?`, `themeName` | `symbol -> theme` |
| `theme-company` | `ticker?`, `companyName?`, `themeName` | `symbol -> theme` |
| `control` | `ticker?`, `controlledName` | `symbol -> company` |
| `theme-control` | `ticker?`, `controlledName` | `symbol -> company` |
| `shared-theme` | `symbols[]`, `themeName` | each `symbol -> theme` |
| `cross-control` | `controllerTicker`, `controlledTicker`, `controlledName?` | `controller symbol -> controlled symbol` |

`ticker`가 없으면 현재 패널 symbol을 사용한다. 위 표에 없는 `relationType`은 무시한다.

## 프론트 그래프 모델

```ts
type OntologyGraphNodeKind = "symbol" | "theme" | "company";
type OntologyGraphEdgeKind = "theme" | "control" | "shared-theme" | "cross-control";

type OntologyGraphData = {
  symbol: string;
  nodes: OntologyGraphNode[];
  edges: OntologyGraphEdge[];
  generatedAt: string;
};
```

그래프 렌더링은 현재 가벼운 SVG 기반이다. D3 force simulation은 사용하지 않는다.
