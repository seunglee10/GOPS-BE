# GOPS Documentation

GOPS는 실시간 시장 데이터를 기반으로 차트와 분석 화면을 구성하고, 사용자의 자연어 요청을 차트 변경과 분석 흐름으로 연결하는 주식 트레이딩 플랫폼이다.

이 문서는 구현을 위한 프로젝트 기준점이다. 팀원별 세부 스펙은 [spec/README.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/README.md)에서 확인한다.

## 문서 상태

- 현재 스펙 문서는 구현 중 변경될 수 있는 참고용 문서다.
- 구현 계획은 문서별로 단계적으로 확정한다.
- 차트와 프론트엔드 설계는 [spec/10-chart/gops-chart-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/10-chart/gops-chart-spec.md)를 1차 기준으로 삼는다.
- 다른 팀원 스펙은 시장 데이터, 주문, 인프라, 보안 경계를 확인하기 위한 참조 문서로 함께 사용한다.

## 제품 방향

GOPS는 차트, 시장 데이터, 분석 대화, 주문 흐름을 하나의 작업 환경 안에서 연결한다.

사용자는 종목 상세 화면에서 실시간 차트와 관련 정보를 확인하고, 자연어 또는 직접 조작으로 차트의 종목, 기간, 비교 대상, 보조지표를 변경한다. AI 응답은 전역 auto toggle이 꺼져 있으면 사용자가 검토하고 승인할 수 있는 제안으로 다루고, 켜져 있으면 검증된 command 묶음으로 적용하되 undo/redo 가능한 변경으로 기록한다. 단, drawing과 comparison 제안은 auto toggle과 무관하게 preview-first로 표시한 뒤 사용자가 차트 패널에서 직접 적용한다.

분석 과정에서 발견한 종목이나 ETF는 별도의 주문 시스템 흐름으로 연결될 수 있다. 주문의 멱등성, 체결 상태, 계좌 보안 같은 거래 관련 책임은 주문 스펙에서 다룬다.

## 핵심 사용자 흐름

1. 사용자가 관심 종목을 선택한다.
2. 프론트엔드가 초기 차트 데이터와 실시간 업데이트를 표시한다.
3. 사용자가 UI 도구 또는 자연어로 차트 변경을 요청한다.
4. 차트 변경은 `ChartDocument`와 `Command Engine`을 통해 검증되고 적용된다.
5. AI가 제안한 변경은 전역 auto toggle 정책에 따라 proposal 대기 또는 검증된 command 묶음으로 반영된다.
6. 분석 중 거래가 필요한 경우 주문 시스템의 별도 흐름으로 넘어간다.

## 현재 구현 중심

현재 우리 담당 범위는 GOPS의 차트와 프론트엔드 시작점이다.

현재 프론트엔드 기준은 desktop Bento Grid workspace다. 모바일 전용 화면, 모바일 viewport 레이아웃, 하단 rail/overlay 같은 모바일 UI 변형은 아직 고려하지 않는다. 반응형 검증은 같은 desktop workspace 안에서 panel 크기가 바뀌는 경우를 우선한다.

현재 구현 상태명은 다음으로 고정한다.

```text
Chart Tool Runtime V1 core implementation baseline + validation hardening backlog
```

이 이름은 data-coordinate drawing, comparison overlay, preview-first LLM proposal, Agent 01 chart chat, panel-local chart undo/redo가 핵심 동작 기준선으로 구현됐다는 뜻이다. Playwright/browser regression, multi-chart browser scenario, `/ref/references` behavior comparison, real provider 전환 정책은 아직 hardening backlog로 남긴다.

우선적으로 정리해야 할 기준은 다음과 같다.

- 실시간 시장 데이터가 차트 상태로 들어오는 계약
- 차트 상태를 표현하는 문서 모델
- 사용자 조작과 AI 제안을 같은 방식으로 처리하는 command 구조
- 차트 패널, 도구, 렌더링, proposal preview를 포함한 프론트엔드 UX
- Agent 01 단독 chart command chat, SystemArea agent panel, symbol-only reference token의 현재 scaffold
- LLM이 사용할 수 있는 chart tool/capability manifest와 의미 있는 도구 조합 기준
- Custom Canvas 차트가 실제로 올바르게 그려지는지 반복 확인하는 렌더링 검증 루프
- 시장 데이터, 주문, 인프라 스펙과 충돌하지 않는 책임 경계

## 문서 구조

- [architecture/service-boundaries.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/architecture/service-boundaries.md): 서비스 경계, shared chart contract 위치, Chart Tool Runtime V1 core baseline 기준
- [process/codex-workflow.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/process/codex-workflow.md): Codex 멀티 채팅 협업 운영 규칙
- [planning/frontend-layout-runtime.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/planning/frontend-layout-runtime.md): Bento Grid 기반 프론트엔드 레이아웃 런타임 계획
- [planning/chart-rendering-runtime.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/planning/chart-rendering-runtime.md): Custom Canvas 기반 차트 렌더링 런타임 요구 기준
- [planning/chart-rendering-milestones.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/planning/chart-rendering-milestones.md): 차트 렌더링 도구 구현 마일스톤
- [planning/chart-tool-runtime-v1.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/planning/chart-tool-runtime-v1.md): 조작 가능한 차트 분석 도구 런타임 V1 기준
- [planning/chart-tool-runtime-milestones.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/planning/chart-tool-runtime-milestones.md): Chart Tool Runtime V1 구현 마일스톤
- [planning/chart-tool-runtime-goal-brief.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/planning/chart-tool-runtime-goal-brief.md): Forge Extra High Goal 구현 요청용 brief
- [spec/README.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/README.md): 스펙 문서 색인
- [spec/00-integrated/gops-integrated-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/00-integrated/gops-integrated-spec.md): 팀원별 스펙 통합 초안
- [spec/10-chart/gops-chart-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/10-chart/gops-chart-spec.md): 차트와 프론트엔드 담당 스펙
- [spec/20-market-data/market-data-pipeline-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/20-market-data/market-data-pipeline-spec.md): 시장 데이터 파이프라인 스펙
- [spec/30-orders](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/30-orders): 주문 시스템 스펙
- [spec/40-infrastructure/devops-architecture-spec.md](/Users/helixho/Desktop/JUNGLE/22 NaManMu/02 POC/Chart/chart_plz/docs/spec/40-infrastructure/devops-architecture-spec.md): 인프라와 운영 스펙
