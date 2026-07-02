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

GitHub Actions는 GitHub 저장소 안의 `.github/workflows/*.yml` 파일로 빌드, 테스트, 배포 같은 자동화를 실행하는 기능이다. 이 프로젝트는 현재 `.github/workflows/deploy-dev.yml`에서 dev/test 브랜치 배포를 처리한다.

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

## Existing GitHub Actions Flow

현재 dev/test 배포 workflow는 `.github/workflows/deploy-dev.yml`을 기준으로 한다.

흐름:

1. `dev`, `deploy/**`, `test/**` branch push 또는 manual dispatch로 시작한다.
2. `scripts/aws/detect-changed-services.sh`가 변경된 service를 감지한다.
3. AWS OIDC role을 assume한다.
4. ECR repository를 확인하거나 생성한다.
5. Amazon ECR에 로그인한다.
6. Docker Buildx를 설정한다.
7. commit SHA 기반 image tag를 만든다.
8. `scripts/aws/build-and-push-images.sh`로 선택된 image를 build/push한다.
9. `aws eks update-kubeconfig`로 cluster 접근을 설정한다.
10. `scripts/aws/update-ci-image-tags.sh`가 CI overlay image tag를 갱신한다.
11. `kubectl apply -k` server-side dry run을 실행한다.
12. `kubectl apply -k`로 app workload를 배포한다.
13. `kubectl rollout status`로 rollout을 확인한다.
14. frontend/backend public endpoint smoke test를 실행한다.
15. 실패하면 `kubectl rollout undo`로 rollback한다.

ECR은 Amazon Elastic Container Registry의 줄임말이다. Docker image를 저장하는 AWS registry다. GitHub Actions에서 image를 build한 뒤 ECR에 push하고, EKS pod는 그 image를 pull해서 실행한다.

Kustomize는 Kubernetes manifest를 base와 overlay로 나눠 관리하는 도구다. GOPS는 `infra/k8s/base`에 공통 manifest를 두고, `infra/k8s/overlays/*`에서 AWS/dev/CI 환경 차이를 반영한다.

IRSA는 IAM Roles for Service Accounts의 줄임말이다. EKS pod가 AWS credential을 직접 들고 있지 않아도 특정 AWS 권한을 가진 IAM role로 동작하게 해준다.

## Deployment Rules

- 모든 Deployment는 readiness/liveness/startup probe를 가진다.
- rolling update 기본값은 `maxUnavailable=0`, `maxSurge=1`이다.
- resource request/limit을 명시한다.
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
