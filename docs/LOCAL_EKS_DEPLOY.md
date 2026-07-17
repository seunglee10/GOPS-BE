# Local EKS Deploy

팀원은 GitHub Actions 대기열을 쓰지 않고 자기 로컬 컴퓨터 리소스로 dev EKS에
배포할 수 있다. 기본 명령어는 하나다.

```bash
AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh
```

이 스크립트는 현재 로컬 브랜치나 미커밋 변경을 배포하지 않는다. 기본값은
`git fetch origin dev`로 원격 `origin/dev` 최신 commit을 가져오고, 임시
`git worktree`에서 그 commit만 checkout해서 빌드한다. 검증용 원격 브랜치는
`REMOTE_BRANCH`, push하지 않은 로컬 commit은 `LOCAL_REF`로 명시한다. 어느 경우든
미커밋 변경은 배포에 포함하지 않는다.

## Required Local Tools

- Docker Desktop: 애플리케이션을 Docker image로 빌드한다. Docker image는 앱
  코드와 실행 환경을 한 덩어리로 포장한 배포 단위다.
- AWS CLI v2: AWS API를 터미널에서 호출하는 공식 도구다. 여기서는 ECR login,
  EKS kubeconfig 갱신, Secrets Manager 조회에 사용한다.
- kubectl: Kubernetes 클러스터에 명령을 보내는 CLI다. 여기서는 EKS에 manifest를
  dry-run/apply하고 rollout 상태를 확인한다.
- git: 원격 `origin/dev` commit을 가져오고 임시 worktree를 만든다.

## First-Time Setup

각 팀원은 공유 access key 대신 자기 AWS profile을 사용한다.

```bash
aws configure --profile gops-dev
aws sts get-caller-identity --profile gops-dev
```

배포 권한에는 최소한 ECR push, EKS cluster 조회, dev namespace Kubernetes 배포,
`icon/logodev` Secrets Manager 읽기 권한이 필요하다.

Docker가 켜져 있는지도 확인한다.

```bash
docker info
```

## Normal Deploy

원격 `dev`에 push된 코드만 배포된다.

```bash
AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh
```

스크립트 동작:

1. `origin/dev` 최신 SHA를 고정한다.
2. EKS `ConfigMap/gops-dev-deploy-state`에서 서비스별 마지막 성공 SHA를 읽는다.
3. 서비스별 baseline과 `origin/dev` 사이의 diff를 기존 service mapping으로 분석한다.
4. 변경된 서비스 image만 로컬 Docker로 빌드해 ECR에 push한다.
5. 임시 worktree의 kustomize overlay만 수정해 EKS에 server-side dry-run/apply한다.
6. 선택된 Deployment rollout과 frontend/backend smoke test를 확인한다.
7. 성공하면 선택된 서비스의 `service.<name>.lastSuccessfulSha`를 최신 SHA로 갱신한다.

서비스별 deploy state가 없으면 먼저 legacy `lastSuccessfulSha`를 확인한다. 단,
legacy `lastSuccessfulServices`에 해당 서비스가 들어 있을 때만 그 SHA를 baseline으로
사용한다. 그래도 baseline이 없으면 현재 EKS primary Deployment의 image tag를 읽어
baseline으로 사용한다. 이 fallback도 실패하면 해당 서비스는 안전하게 재빌드 대상이
된다.

이 구조에서는 backend만 성공 배포해도 frontend의 미배포 변경이 사라지지 않는다.
다음 실행 때 frontend의 서비스별 baseline부터 다시 diff를 계산하기 때문이다.

## Dry Run

실제 image push나 Kubernetes apply 없이 target/diff/server-side dry-run을 확인한다.

```bash
DRY_RUN=true AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh
```

원격 feature branch를 검증 배포할 때도 먼저 dry-run한다. 로컬 checkout이나
미커밋 파일이 아니라 지정한 `origin/<branch>` commit이 대상이다.

```bash
REMOTE_BRANCH=codex/expand-chart-patterns \
FORCE_SERVICES=frontend,agent-orchestrator \
CHART_INTERPRETATION_ONLY=true \
DRY_RUN=true \
AWS_PROFILE=gops-dev \
./scripts/aws/deploy-dev-local.sh
```

push하지 않은 로컬 `dev` commit을 배포할 때도 먼저 같은 검증을 거친다.

```bash
LOCAL_REF=dev \
FORCE_SERVICES=frontend,backend,simulator \
DRY_RUN=true \
AWS_PROFILE=gops-dev \
./scripts/aws/deploy-dev-local.sh
```

실제 배포는 위 명령에서 `DRY_RUN=true`만 제거한다.

`CHART_INTERPRETATION_ONLY=true`는 기존 Geometry 자산을 읽는 reader 호환 확인 전용
배포 경계다. 전체 Kustomize overlay를 apply하지 않고 다음 Deployment의 image만
교체한다.

```text
gops-frontend
agent-analysis-worker
agent-orchestrator
```

이 경로는 `chart-asset-builder`를 갱신하지 않으므로 commentary writer/prompt 변경을
검증하거나 개발 패널에서 자산을 재생성하는 용도로 사용하지 않는다. writer 변경은 일반
`agent-orchestrator` 배포로 builder까지 rollout한 뒤
`scripts/aws/preflight-chart-commentary-aws.sh`를 통과시킨다.

따라서 같은 agent image를 공유하는 `chart-asset-builder`, `chart-geometry-build`
CronJob, migration/maintenance Job과 다른 agent workload는 변경하지 않는다. 이 모드는
`FORCE_SERVICES=frontend,agent-orchestrator`를 반드시 함께 사용하며 migration, cache
rebuild, platform apply option이 하나라도 켜져 있으면 시작 전에 실패한다. 실제 배포도
위 명령에서 `DRY_RUN=true`만 제거해 같은 경계를 유지한다.

## Emergency Overrides

전체 app image를 강제로 다시 빌드/배포한다.

```bash
FORCE_SERVICES=all AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh
```

일부 서비스만 강제로 배포할 수도 있다.

```bash
FORCE_SERVICES=frontend,backend AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh
```

시뮬레이터 image와 기본 0-replica Deployment만 배포할 수도 있다.

```bash
FORCE_SERVICES=simulator AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh
```

배포 후 실제 시연 경로를 켜고 끄는 명령은 별도다.

```bash
AWS_PROFILE=gops-dev ./scripts/aws/start-dev-simulator.sh
AWS_PROFILE=gops-dev ./scripts/aws/stop-dev-simulator.sh
```

최초 한 번은 실제 틱 데이터셋을 적재한 뒤 SIM을 시작한다.

```bash
AWS_PROFILE=gops-dev ./scripts/aws/run-simulator-replay-import.sh
AWS_PROFILE=gops-dev ./scripts/aws/start-dev-simulator.sh
```

Order migration은 `order-worker` 선택 시, Chart migration은 `agent-orchestrator`
선택 시 app rollout 전에 자동 실행된다. `agent-orchestrator`를 선택하면 두 migration
image가 함께 선택된다. news cache rebuild만 명시적 switch가 필요하다.
단, 위 `CHART_INTERPRETATION_ONLY` 경로는 이 일반 결합을 적용하지 않는다.

```bash
FORCE_SERVICES=order-worker AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh
FORCE_SERVICES=agent-orchestrator AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh
REBUILD_NEWS_CACHE=true FORCE_SERVICES=market-storage AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh
```

## Failure Behavior

- Docker build나 ECR push가 실패하면 EKS apply 전이므로 클러스터 상태는 바뀌지 않는다.
- 자동 order/chart migration이 실패하면 app workload apply 전에 중단한다.
- agent rollout 뒤 IRSA snapshot canary write가 실패하면 배포를 실패 처리하고 rollback을 시도한다.
- EKS apply 이후 rollout, smoke, rebuild 단계가 실패하면 선택된 Deployment에
  `kubectl rollout undo`를 시도한다.
- 실패한 실행은 `gops-dev-deploy-state`를 갱신하지 않는다.
- 로컬 작업 폴더는 수정하지 않고, 임시 worktree는 종료 시 삭제한다.

GitHub Actions `.github/workflows/deploy-dev.yml`은 비상용 백업 경로로 유지한다.
