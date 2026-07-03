# 1. AI / Agent

## Mission

근거 기반 멀티 Agent 분석을 구현한다.

이 역할은 "AI가 결론을 말하는 기능"만 만드는 것이 아니다. 각 Agent가 어떤 데이터를 근거로 판단했는지 남기고, 다른 Agent가 그 근거를 반박하거나 검증할 수 있게 만드는 것이 핵심이다.

## Owns

- Agent Runtime
- 차트 Agent, 뉴스 Agent, 펀더멘탈 Agent, 포트폴리오 Agent, 검증 Agent
- `EvidenceItem` 사용 규칙
- Agent event model
- Agent finding, challenge, consensus, finalized report
- Agent proposal이 주문으로 바로 이어지지 않도록 하는 guardrail
- OpenAI API key 같은 LLM provider secret 사용 방식

LLM은 Large Language Model의 줄임말이다. 자연어를 이해하고 생성하는 모델을 뜻한다. 이 프로젝트에서는 Agent가 뉴스, 차트, 펀더멘탈, 포트폴리오 정보를 읽고 요약/비판/합의하는 데 사용한다.

## Does Not Own

- Alpaca news 원문 수집과 S3 저장
- market tick, candle, quote, bar 수집과 저장
- SEC filing 원문 수집과 S3 저장
- UI 차트 렌더링
- 사용자의 실제 주문 제출
- Kubernetes 배포 설정

뉴스와 펀더멘탈 원문 저장은 3번 담당자가 맡는다. AI 담당자는 저장된 데이터와 projection을 근거로 읽는다.

## Main Paths

- `systems/agent-orchestration/`
- `systems/agent-orchestration/shared/gops_agents/`
- `systems/agent-orchestration/pods/agent-orchestrator/`
- `systems/agent-orchestration/pods/event-detector/`
- `systems/agent-orchestration/pods/notification-publisher/`
- `shared/chart-contract/` 후보

## Source Sections

`docs/v2/gops-v2-architecture.md`에서 먼저 볼 섹션:

- `5.7 Agent Runtime`
- `12. Chart Agent Output`
- `20. Agent Event Model`
- `23.1 Billing And Payment`
- `24. Observability`
- `25. Testing Strategy`
- `26. Security`
- `27. Assumptions`
- `28. Open Decisions`
- `29. Scaffold Structure`

## Contracts Consumed

AI 담당자는 다음 데이터를 직접 생성하지 않고 소비한다.

- Chart candle/indicator projection
- Latest quote/live candle cache
- Alpaca `NewsEvent`
- SEC fundamentals summary/time series
- Portfolio snapshot
- Order state event summary

## Contracts Produced

AI 담당자는 다음 결과를 제공한다.

- `agent-runs.events` Kafka event
- Agent run state
- Agent finding
- Agent challenge
- Agent consensus
- Agent finalized report
- `proposal.created`

Kafka는 이벤트 스트리밍 플랫폼이다. 어떤 일이 발생했는지를 topic이라는 통로에 발행하면 여러 worker가 그 이벤트를 구독해서 처리할 수 있다. 여기서는 Agent 진행 상태를 API, UI, 저장 worker가 함께 볼 수 있게 만드는 데 필요하다.

## Evidence Rules

모든 Agent finding은 하나 이상의 `EvidenceItem`과 연결되어야 한다.

`EvidenceItem`은 "이 판단의 근거가 된 데이터 조각"이다. 예를 들어 특정 시간 구간의 거래량, 특정 뉴스 기사, SEC filing, 포트폴리오 snapshot이 될 수 있다.

최소 필드:

- `id`
- `type`
- `source`
- `symbol`
- `asOf`
- `stalenessMs`
- `artifactUri`
- `summary`

규칙:

- 근거 없는 finding은 만들지 않는다.
- 오래된 데이터는 `stale` 경고를 붙인다.
- 과장된 해석은 검증 Agent가 `overstated`로 표시할 수 있어야 한다.
- 충돌하는 근거가 있으면 `conflicting` 상태를 남긴다.

## Guardrails

- Agent Runtime에는 KIS credential을 주지 않는다.
- Agent Runtime은 order API를 호출할 수 없다.
- Agent는 주문 티켓 prefill을 제안할 수는 있지만 submit할 수 없다.
- `agent-runtime -> kis-adapter` 직접 접근은 Kubernetes NetworkPolicy로 차단한다.
- Agent가 만든 제안은 사용자가 직접 확인하고 버튼을 눌렀을 때만 주문으로 이어진다.

## First Implementation Checklist

- Agent event type을 코드와 문서에서 맞춘다.
- `EvidenceItem` schema를 안정화한다.
- Agent run state reducer를 테스트한다.
- 근거 없는 finding 생성 시 실패하는 테스트를 만든다.
- Agent output이 UI와 API에서 읽을 수 있는 형태인지 2번, 5번 담당자와 확인한다.
- OpenAI secret 주입 방식은 4번 담당자와 확인한다.

## Handoffs

- 2번 Frontend: Agent 상태, finding, warning, consensus를 어떻게 표시할지 합의한다.
- 3번 Data Pipeline: news/fundamental/chart evidence의 `artifactUri`, `asOf`, `stalenessMs` 의미를 맞춘다.
- 4번 Infra: Agent pod image, secret, NetworkPolicy, rollout 상태를 맞춘다.
- 5번 Backend: Agent API, SSE/WebSocket event forwarding, report 조회 contract를 맞춘다.
