# 4. Infra / Platform / CI-CD

## Mission

GOPS v2가 AWS/EKS에서 안전하게 빌드, 배포, 운영되도록 platform과 delivery 경로를 관리한다.

이 역할은 서버 코드를 직접 구현하는 역할이 아니다. 기능팀이 만든 pod/job/image/env/topic/secret 변경이 실제 dev/test/production-like 환경에 같은 방식으로 올라가도록 만드는 역할이다.

## Owns

- AWS resource handoff
- EKS cluster runtime
- Kubernetes manifest
- Kustomize overlay
- Dockerfile과 image boundary
- ECR repository
- Terraform
- IRSA
- AWS Secrets Manager 연동
- External Secrets Operator manifest
- NetworkPolicy
- resource request/limit
- readiness/liveness/startup probe
- observability, alert
- GitHub Actions workflow
- CI/CD rollout, smoke test, rollback

기본 dev/test 배포는 `scripts/aws/deploy-dev-local.sh`를 사용한다. 이 스크립트는
팀원 로컬 컴퓨터에서 Docker build를 실행하지만, 배포 기준은 항상 원격
`origin/dev` 최신 commit이다. 로컬 미커밋 변경이나 현재 checkout 브랜치는 배포에
섞이지 않는다. GitHub Actions는 GitHub 저장소 안의 `.github/workflows/*.yml`
파일로 빌드, 테스트, 배포 같은 자동화를 실행하는 기능이며, 이 프로젝트에서는
`.github/workflows/deploy-dev.yml`을 비상용 수동 배포 경로로 유지한다.

CI/CD는 Continuous Integration/Continuous Delivery의 줄임말이다. 코드를 합치고 검증하고 배포하는 과정을 자동화한다는 뜻이다.

## Does Not Own

- Agent 분석 로직
- 차트 렌더링 로직
- Alpaca/SEC business parsing logic
- API route business validation
- KIS 주문 domain 로직

단, 위 기능들이 새 pod, job, image, env, secret, topic, storage contract를 만들면 배포 경로 반영은 4번 담당자가 책임진다.

## Main Paths

- `.github/workflows/`
- `infra/`
- `infra/docker/`
- `infra/k8s/base/`
- `infra/k8s/overlays/`
- `infra/aws/terraform/`
- `infra/aws/values/`
- `platform/`
- `scripts/aws/`
- `docs/IMAGE_STRATEGY.md`
- `docs/ENVIRONMENT.md`

## Source Sections

`docs/v2/gops-v2-architecture.md`에서 먼저 볼 섹션:

- `6. AWS Initial Sizing`
- `7. Kubernetes Deployment`
- `15. S3 Storage`
- `19. Kafka Topics`
- `23.3 Market Data Reliability And Cache`
- `23.4 Operational Alerts`
- `24. Observability`
- `26.2 Secret Handling`
- `26.4 Infrastructure Security`
- `29.4 Pod And Job Map`
- `29.5 Platform Contracts To Add Or Keep Current`

## Local Dev Deploy Flow

현재 기본 dev/test 배포는 `scripts/aws/deploy-dev-local.sh`를 기준으로 한다.

흐름:

1. 팀원이 로컬에서 `AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh`를 실행한다.
2. 스크립트가 `git fetch origin dev`로 원격 최신 commit을 가져오고, 임시
   `git worktree`에서 그 SHA를 checkout한다.
3. EKS `ConfigMap/gops-dev-deploy-state`의 `lastSuccessfulSha`부터
   `origin/dev`까지의 diff를 `scripts/aws/detect-changed-services.sh`가 분석한다.
   상태가 없으면 첫 실행으로 보고 전체 app image를 선택한다.
4. `FORCE_SERVICES=frontend,backend`를 넣으면 그 service만 선택하고,
   `FORCE_SERVICES=all`을 넣으면 전체 app image를 강제로 다시 빌드한다.
5. `APPLY_PLATFORM_MANIFESTS=true`이면 전용 NodePool과 in-cluster platform
   manifest를 함께 적용할 준비를 한다. 기본값은 `false`라 일반 앱 배포는
   platform을 건드리지 않는다.
6. 팀원별 AWS profile로 `aws sts get-caller-identity`와
   `aws eks update-kubeconfig`를 실행해 cluster 접근을 설정한다.
7. ECR repository를 확인하거나 생성한다.
8. `frontend`가 선택되면 `scripts/aws/load-logodev-build-env.sh`가 AWS Secrets Manager `icon/logodev`에서 `LOGODEV_PUB_KEY`만 읽어 frontend build env에 넣는다.
9. Amazon ECR에 로그인한다.
10. CI는 commit SHA, 실행 ID, 재시도 번호를 포함하고 로컬 배포는 commit SHA와
    UTC timestamp를 포함하는 고유 image tag를 만든다. ECR tag는 immutable이라
    같은 tag를 덮어쓰지 않는다.
11. `scripts/aws/build-and-push-images.sh`로 선택된 image를 로컬 Docker에서 build/push한다.
12. `APPLY_PLATFORM_MANIFESTS=true`이면 `infra/k8s/base/platform`과 GraphDB
    StatefulSet을 server-side dry-run 후 apply하고, StatefulSet rollout을
    기다린다.
13. `scripts/aws/validate-dedicated-platform.sh`가 `app-agent`, `cache-db`,
    `streaming`, `graphdb`, `clickhouse`, `batch-warm`, 동적 `batch` NodePool과 Stateful pod
    배치를 확인한다.
14. 임시 worktree 안에서 `scripts/aws/update-ci-image-tags.sh`가 CI overlay image tag를 갱신한다. 원래 작업 폴더의 tracked manifest는 수정하지 않는다.
15. Git에 선언된 `alert-evaluator`와 `recommendation-worker` replica를 그대로
    사용한다. live cluster 값을 읽어 manifest를 수정하지 않는다.
16. `kubectl apply -k` server-side dry run을 실행한다.
17. `kubectl apply -k`로 app workload를 배포한다.
18. `kubectl rollout status`로 선택된 image가 사용하는 모든 Deployment
    rollout을 확인한다.
19. `market-storage`가 선택된 배포에서는 `scripts/aws/run-news-cache-rebuild-jobs.sh`가 뉴스 Redis cache rebuild Job을 실행한다.
20. frontend/backend public endpoint smoke test를 실행한다.
21. 성공하면 `gops-dev-deploy-state`에 target SHA, 서비스 목록, 배포자, 시간을 기록한다.
22. 실패하면 `kubectl rollout undo`로 rollback하고 deploy state는 갱신하지 않는다.

GitHub Actions `.github/workflows/deploy-dev.yml`은 비상용 수동 배포 경로다.
`workflow_dispatch`는 GitHub Actions의 수동 실행 트리거이며, branch push는 배포를
시작하지 않는다.

ECR은 Amazon Elastic Container Registry의 줄임말이다. Docker image를 저장하는 AWS registry다. GitHub Actions에서 image를 build한 뒤 ECR에 push하고, EKS pod는 그 image를 pull해서 실행한다.

Kustomize는 Kubernetes manifest를 base와 overlay로 나눠 관리하는 도구다. GOPS는 `infra/k8s/base`에 공통 manifest를 두고, `infra/k8s/overlays/*`에서 AWS/dev/CI 환경 차이를 반영한다.
수동 앱 배포 overlay는 `infra/k8s/base/app`만 상속해야 한다. `infra/k8s/base`의 one-shot/smoke/eval Job은 별도 수동 실행 대상이며, deploy workflow apply 경로에 포함하면 기존 Job의 immutable `spec.template` 때문에 dry-run/apply가 실패할 수 있다.

IRSA는 IAM Roles for Service Accounts의 줄임말이다. EKS pod가 AWS credential을 직접 들고 있지 않아도 특정 AWS 권한을 가진 IAM role로 동작하게 해준다.

## Deployment Rules

- 모든 Deployment는 readiness/liveness/startup probe를 가진다.
- EKS in-cluster app overlay의 rolling update는 `maxUnavailable=1`,
  `maxSurge=0`이다. 고정 크기 `app-agent` NodePool에서 새 Pod를 먼저
  띄우다가 capacity deadlock이 나는 것을 피하기 위해, 기존 Pod 하나를 먼저
  내릴 수 있게 한다.
- resource request/limit을 명시한다.
- EKS stateful clean rebuild는 `docs/EKS_DATA_PRESERVING_REBUILD_PLAN.md`를
  따른다. Postgres, ClickHouse, GraphDB 데이터 손실은 허용하지 않으며,
  Redis/Kafka reset도 component owner의 명시 승인 없이는 기본값이 아니다.
- GitHub Actions의 `apply_platform_manifests`는 PVC 삭제/복원을 수행하지
  않는다. 승인된 데이터 보존 작업 이후 dedicated NodePool/StatefulSet
  manifest를 현재 상태로 수렴시키는 용도다.
- KIS/Alpaca/Google OAuth/OpenAI credential은 image나 ConfigMap에 넣지 않는다.
- secret은 AWS Secrets Manager 또는 Kubernetes Secret으로 주입한다.
- production namespace에서도 KIS demo endpoint만 허용한다.
- `agent-runtime -> kis-adapter` 직접 접근은 차단한다.

## Image Boundary Rules

image가 늘어나면 다음을 같이 갱신한다.

- `docs/IMAGE_STRATEGY.md`
- `infra/docker/*`
- `infra/k8s/base`
- `infra/k8s/overlays/*`
- `.github/workflows/deploy-dev.yml`
- `scripts/aws/lib-gops-images.sh`
- `scripts/aws/build-and-push-images.sh`
- `scripts/aws/detect-changed-services.sh`
- owning system README

## First Implementation Checklist

- 새 pod/job이 생기면 image mapping을 먼저 확인한다.
- CI workflow가 새 service key를 감지하는지 확인한다.
- ECR repository env와 script mapping을 추가한다.
- kustomize base와 CI overlay image tag를 맞춘다.
- `kubectl apply -k --dry-run=server`가 통과해야 한다.
- rollout diagnostics script가 새 deployment를 볼 수 있어야 한다.
- smoke test가 필요한 public endpoint를 명시한다.

## Handoffs

- 1번 AI: OpenAI secret, agent image, NetworkPolicy, rollout health를 맞춘다.
- 2번 Frontend: frontend image build, nginx serving, public smoke endpoint를 맞춘다.
- 3번 Data Pipeline: market/news/storage worker image, Kafka/S3/ClickHouse/Redis env를 맞춘다.
- 5번 Backend: API image, auth/session/order secret, migration job, public backend smoke endpoint를 맞춘다.
