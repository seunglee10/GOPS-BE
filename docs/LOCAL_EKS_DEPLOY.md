# Local EKS Deploy

팀원은 GitHub Actions 대기열을 쓰지 않고 자기 로컬 컴퓨터 리소스로 dev EKS에
배포할 수 있다. 기본 명령어는 하나다.

```bash
AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh
```

이 스크립트는 현재 로컬 브랜치나 미커밋 변경을 배포하지 않는다. 항상
`git fetch origin dev`로 원격 `origin/dev` 최신 commit을 가져오고, 임시
`git worktree`에서 그 commit만 checkout해서 빌드한다.

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

## Emergency Overrides

전체 app image를 강제로 다시 빌드/배포한다.

```bash
FORCE_SERVICES=all AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh
```

일부 서비스만 강제로 배포할 수도 있다.

```bash
FORCE_SERVICES=frontend,backend AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh
```

Order migration이나 news cache rebuild는 관련 image가 선택될 때만 허용된다.

```bash
RUN_ORDER_MIGRATIONS=true FORCE_SERVICES=order-worker AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh
REBUILD_NEWS_CACHE=true FORCE_SERVICES=market-storage AWS_PROFILE=gops-dev ./scripts/aws/deploy-dev-local.sh
```

## Failure Behavior

- Docker build나 ECR push가 실패하면 EKS apply 전이므로 클러스터 상태는 바뀌지 않는다.
- EKS apply 이후 rollout, smoke, rebuild 단계가 실패하면 선택된 Deployment에
  `kubectl rollout undo`를 시도한다.
- 실패한 실행은 `gops-dev-deploy-state`를 갱신하지 않는다.
- 로컬 작업 폴더는 수정하지 않고, 임시 worktree는 종료 시 삭제한다.

GitHub Actions `.github/workflows/deploy-dev.yml`은 비상용 백업 경로로 유지한다.
