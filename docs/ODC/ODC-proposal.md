# ODC Proposal

## 온톨로지 리포트 요청

| 필드 | 필수 | 의미 | 활용 |
| --- | --- | --- | --- |
| `symbol` | 예 | 온톨로지 관계를 조회할 기준 종목 티커 | 온톨로지 그래프: 기준 symbol 노드 생성 및 relation 해석 기준 |
| `prompt` | 아니오 | 사용자가 온톨로지 관계를 요청한 자연어 질의 | 온톨로지 그래프: 백엔드가 ontology provider를 선택하거나 관계 범위를 좁힐 때 활용 |
| `context` | 아니오 | 프론트가 제공하는 화면/패널 맥락 | 온톨로지 그래프: 백엔드 분석 리포트 생성 보조 정보 |

비고: 프론트는 GraphDB, SPARQL, zip 파일을 직접 읽지 않는다. 프론트는 백엔드가 완성한 `AnalysisReport.providerEvidence`만 사용한다.

## 온톨로지 리포트 응답

| 필드 | 필수 | 의미 | 활용 |
| --- | --- | --- | --- |
| `providerEvidence` | 예 | 백엔드 provider들이 생성한 evidence 배열 | 온톨로지 그래프: `provider="ontology"` 항목만 필터링 |
| `providerEvidence[].provider` | 예 | evidence 제공자 | 온톨로지 그래프: `"ontology"`일 때만 사용 |
| `providerEvidence[].status` | 예 | evidence 상태 | 온톨로지 그래프: `"available"`일 때만 사용 |
| `providerEvidence[].title` | 아니오 | evidence 제목 | 온톨로지 패널: 관계 목록 보조 텍스트 |
| `providerEvidence[].summary` | 아니오 | evidence 요약 | 온톨로지 패널: 관계 목록 보조 텍스트 |
| `providerEvidence[].raw` | 예 | relationType별 원천 필드 | 온톨로지 그래프: 노드와 엣지 생성 |

비고: 프론트는 `provider === "ontology"`이고 `status === "available"`인 evidence만 그래프로 변환한다. `provider !== "ontology"`이거나 `status !== "available"`인 evidence는 그래프 변환에서 제외한다. 온톨로지 evidence가 없으면 프론트는 `관계 분석 결과가 아직 없습니다`를 표시한다.

## relationType별 raw 필드

| relationType | 필드 | 필수 | 의미 | 그래프 활용 |
| --- | --- | --- | --- | --- |
| `theme` | `ticker` | 아니오 | 테마와 연결된 종목 티커 | `ticker`가 없으면 요청 `symbol`을 symbol 노드로 사용 |
| `theme` | `themeName` | 예 | 연결된 테마명 | `symbol -> theme` 엣지 생성 |
| `theme-company` | `ticker` | 아니오 | 테마와 연결된 종목 티커 | `ticker`가 없으면 요청 `symbol`을 symbol 노드로 사용 |
| `theme-company` | `companyName` | 아니오 | 종목 회사명 | 패널 보조 텍스트로만 사용 가능 |
| `theme-company` | `themeName` | 예 | 연결된 테마명 | `symbol -> theme` 엣지 생성 |
| `control` | `ticker` | 아니오 | 지배/관계의 기준 종목 티커 | `ticker`가 없으면 요청 `symbol`을 symbol 노드로 사용 |
| `control` | `controlledName` | 예 | 관계 대상 회사명 | `symbol -> company` 엣지 생성 |
| `theme-control` | `ticker` | 아니오 | 지배/관계의 기준 종목 티커 | `ticker`가 없으면 요청 `symbol`을 symbol 노드로 사용 |
| `theme-control` | `controlledName` | 예 | 관계 대상 회사명 | `symbol -> company` 엣지 생성 |
| `shared-theme` | `symbols` | 예 | 같은 테마를 공유하는 종목 티커 배열 | 각 `symbol -> theme` 엣지 생성 |
| `shared-theme` | `themeName` | 예 | 공유 테마명 | theme 노드 생성 |
| `cross-control` | `controllerTicker` | 예 | 관계를 가진 기준 종목 티커 | source symbol 노드 생성 |
| `cross-control` | `controlledTicker` | 예 | 관계 대상 종목 티커 | target symbol 노드 생성 |
| `cross-control` | `controlledName` | 아니오 | 관계 대상 회사명 | edge label 또는 보조 텍스트로 사용 가능 |

비고: 위 relationType 외의 값은 프론트가 무시한다. 새 relationType을 표시하려면 백엔드 응답 계약과 프론트 mapper, 이 ODC 문서를 함께 갱신한다.
