# GOPS Docs

Reference docs for future implementation. The root `README.md` stays short;
repo-wide Codex rules live in root `AGENTS.md`; deeper project docs live here.

```mermaid
flowchart TD
  README["docs/README.md"]
  ARCH["AGENT_ARCHITECTURE.md<br/>에이전트 자체 설명"]
  POLICY["AGENT_ANALYSIS_QUERY_POLICY.md<br/>분석 쿼리/응답 정책"]
  BE["AGENT_BACKEND_INTEGRATION.md<br/>백엔드 연동"]
  FE["AGENT_FRONTEND_INTEGRATION.md<br/>프런트 연동"]
  AWS["AGENT_AWS_BUILD.md<br/>AWS 빌드/배포"]

  README --> ARCH
  README --> POLICY
  README --> BE
  README --> FE
  README --> AWS
  POLICY --> ARCH
  POLICY --> FE
  FE --> BE
  BE --> ARCH
  ARCH --> AWS
```

| File | Purpose |
| --- | --- |
| `PRODUCT_CONTEXT.md` | Product direction and current/future scope boundary. |
| `CHART_DATA_REBUILD_PLAN.md` | Source-of-truth plan for the on-demand chart data rebuild. This wins over older chart, market-data, preload, S3, Redis, and Kafka notes. |
| `CHART_DATA_CONTRACTS.md` | Current chart fact/key/topic/table/prefix matrix plus retention migration and after-deploy operator runbook. |
| `plans/orderflow-bidask-stabilization/README.md` | Bid/Ask 차트 당일 인트라데이 전환, 오더플로우 Redis 저장 모델(캔들형 append+덮어쓰기), 집계 검증, Redis 경량화 4개 워크스트림 실행 계획 (2026-07-10 결정 기록 포함). |
| `STRUCTURE_GUIDE.md` | Folder placement rules for future code. |
| `ARCHITECTURE.md` | Current system, pod/job, and platform relationships. |
| `IMAGE_STRATEGY.md` | Docker image boundaries and naming rules. |
| `ENVIRONMENT.md` | Env, secret, and platform contracts. |
| `LOCAL_EKS_DEPLOY.md` | One-command local deploy runbook for origin/dev to shared dev EKS. |
| `EKS_DATA_PRESERVING_REBUILD_PLAN.md` | Data-preserving EKS clean rebuild runbook for dedicated NodePools and restored PVCs. |
| `AGENT_ORCHESTRATION_IMPLEMENTATION.md` | Summary of the role-based multi-agent implementation and Docker validation. |
| `STOCK_RECOMMENDATION_PANEL.md` | 장중 매수 추천 패널 구현 흐름, 점수화 로직, API/DB/worker/프런트 계약. |
| `AGENT_ARCHITECTURE.md` | `AgentOrchestrator`, role agents, snapshots, synthesis, provider boundary, `EvidenceItem` 계약. |
| `AGENT_ANALYSIS_QUERY_POLICY.md` | 우선 처리할 분석 쿼리 종류, 결론 중심 답변 형식, 신뢰도 표시, 내부 GraphDB 사용 정책. |
| `AGENT_BACKEND_INTEGRATION.md` | Backend API, idempotency, Kafka async path, Redis report store, polling/SSE/WebSocket 계약. |
| `AGENT_FRONTEND_INTEGRATION.md` | 프런트 request shape, `analysisId`, polling/SSE, report rendering, layout/chart proposal 처리. |
| `AGENT_AWS_BUILD.md` | `gops-agent-orchestrator` image, ECR/EKS, Kafka, Redis/Valkey, ClickHouse, GraphDB, S3, secrets, smoke checks. |
| `../AGENTS.md` | Codex/contributor rules for this repo. |

Old long-form specs were removed to avoid stale, conflicting guidance.
If a future long-form spec is needed, add it under `docs/` with a clear owner
and date. Agent-specific handoff content should stay in the `AGENT_*` documents
listed above.

For chart data work, do not follow older notes that require a preset symbol
universe chart preload, S&P500-wide tick/quote collection, raw S3 replay as a
normal read path, or a Kafka topic layout that differs from the Mermaid. SIP
S&P500 bars/statuses baseline collection is the narrow runtime exception
documented in `CHART_DATA_REBUILD_PLAN.md`.
