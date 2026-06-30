# Alpaca Market Data Stabilization Handoff

이 폴더의 루트 문서는 팀 공유와 병합 판단을 위한 문서입니다. Goal 진행 중 작성된 긴 실행 로그와 마일스톤 기록은 `archive/`에 보존하되, 병합 기준으로 사용하지 않습니다.

## 팀이 먼저 읽을 문서

1. [`s3-first-backfill-goal-plan.md`](s3-first-backfill-goal-plan.md)
   - 다음 Goal 모드에서 그대로 실행할 20개 종목 canonical rebuild 계획입니다.
   - S3 초기화, ClickHouse/Redis reset, `v2 + split`, 3년 범위, 중복 방지, S3-first backfill 계약을 담습니다.

2. [`market-data-stabilization-share.md`](market-data-stabilization-share.md)
   - 이번 안정화에서 확정된 기능 계약, 데이터 흐름, 검증 기준입니다.
   - 시장데이터 백엔드, API, 차트 런타임이 지켜야 할 동작만 담습니다.

3. [`team-merge-guide.md`](team-merge-guide.md)
   - 팀원들이 이미 진행한 프론트엔드/에이전트 작업과 병합할 때의 기준입니다.
   - UI/에이전트 구조는 팀원 브랜치를 우선하고, 이번 작업의 시장데이터 기능 계약만 필요한 단위로 포팅합니다.

## 아카이브

`archive/`에는 Goal 수행 과정에서 작성된 상세 계획, 마일스톤, 검증 로그가 있습니다.

- `archive/stabilization-plan.md`
- `archive/milestone-0-baseline.md`
- `archive/milestone-1-live-path.md`
- `archive/milestone-2-universe-hot-ranking.md`
- `archive/milestone-3-serving-readiness.md`
- `archive/milestone-4-backfill-gapfill.md`

아카이브 문서는 이력과 근거 확인용입니다. 팀 병합이나 구현 방향을 결정할 때는 루트의 공유용 문서를 기준으로 삼습니다.

## 공유 원칙

- 팀원 브랜치에 이미 구현된 에이전트, 레이아웃, 패널, 프론트엔드 구조 변경은 기본적으로 유지합니다.
- 이번 Goal에서 보존해야 하는 것은 시장데이터의 기능 계약입니다.
- 프론트엔드 파일 충돌이 생기면 화면 구조를 통째로 덮어쓰지 말고, 차트 데이터 로딩, 실시간 상태, backfill 트리거, Watch/Hot 데이터 계약에 필요한 로직만 옮깁니다.
- S3/ClickHouse/Redis/API/차트 런타임 계약은 한쪽만 부분 적용하면 깨지기 쉬우므로 `team-merge-guide.md`의 순서대로 병합합니다.
- 오래된 S&P500 전체, Hot Top20, `1m 2025-04 cutoff` 전제는 이번 20개 종목 canonical rebuild 계획보다 우선하지 않습니다.
