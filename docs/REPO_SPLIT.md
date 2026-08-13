# 저장소 분리 (gops → gops-backend / gops-frontend)

기존 모노레포 `gops`를 프론트엔드와 백엔드 두 저장소로 분리했습니다.
`git filter-repo`로 **커밋 히스토리를 보존**했으므로 양쪽 모두 해당 파일의
과거 커밋을 그대로 가지고 있습니다.

## 무엇이 어디로 갔는가

| 모노레포 경로 | 이동 위치 |
| --- | --- |
| `apps/gops-frontend/` | gops-frontend 저장소 루트 |
| `apps/chart-engine/` | gops-frontend `packages/chart-engine/` |
| `infra/docker/Dockerfile.gops-frontend` | gops-frontend `docker/Dockerfile` |
| `infra/docker/nginx/gops-frontend.conf` | gops-frontend `docker/nginx/` |
| `systems/market-data/tests/fixtures/chart_assets_v2/{aapl,amzn,wmt}-1d.json` | gops-frontend `fixtures/replay-candles/` (사본, 백엔드에도 원본 유지) |
| `shared/chart-contract/` | **양쪽 모두**. 이 저장소가 원본(SSOT), 프론트는 사본 |
| 그 외 전부 (`systems/`, `platform/`, `infra/`, `docs/`, `scripts/`, `config/`) | 이 저장소 |

## 이 저장소에서 바뀐 것

- `infra/docker/Dockerfile.gops-frontend`, `infra/docker/nginx/gops-frontend.conf` 삭제
  (프론트 저장소가 소유)
- `docker-compose.yml`의 `gops-frontend` 서비스가 `${GOPS_FRONTEND_PATH:-../gops-frontend}`
  를 빌드 컨텍스트로 사용
- `.github/workflows/deploy-dev.yml`의 `quality` job에서 프론트엔드 npm 단계 제거
  (프론트 저장소 CI가 담당)

## 배포 소유권

**프론트엔드 이미지의 빌드·ECR push·롤아웃은 gops-frontend 저장소가 담당합니다.**
이 저장소의 배포 파이프라인에서 `frontend` 서비스를 제거했습니다.

이 저장소에서 제거된 것:

| 파일 | 변경 |
| --- | --- |
| `scripts/aws/lib-gops-images.sh` | `frontend` 서비스 키·Deployment 매핑 제거 |
| `scripts/aws/build-and-push-images.sh` | `Dockerfile.gops-frontend` 특수 처리·정규화 제거 |
| `scripts/aws/detect-changed-services.sh` | frontend 경로 감지 및 `smoke_frontend` 출력 제거 |
| `scripts/aws/deploy-chart-interpretation-images.sh` | frontend Deployment 롤아웃 제거 (agent consumer 2개만 갱신) |
| `scripts/aws/deploy-dev-local.sh` | frontend 선택·logo.dev 시크릿 로드·스모크 제거 |
| `scripts/aws/load-logodev-build-env.sh` | gops-frontend `scripts/`로 이동 |
| `.github/workflows/deploy-dev.yml` | `ECR_FRONTEND_REPO`, logo.dev 단계, 프론트 스모크, npm 단계 제거 |

`infra/k8s`는 이 저장소가 계속 소유하되, CI overlay
(`infra/k8s/overlays/aws-incluster-app-ci`)에서 **gops-frontend Deployment만
`$patch: delete`로 제외**했습니다. 이 overlay를 apply할 때 프론트가 배포한
이미지 태그를 덮어쓰지 않게 하기 위함입니다. Service와 Ingress는 트래픽 경로이므로
그대로 유지되며 이 저장소가 관리합니다.

`infra/k8s/base`는 전체 시스템을 렌더링할 수 있도록 frontend Deployment를
그대로 두었습니다. 다른 overlay(`aws`, `aws-ci`, `aws-incluster-app`)도 손대지
않았으므로 수동 전체 apply 경로는 이전과 동일합니다.

### gops-frontend 저장소에서 해야 할 설정

`.github/workflows/deploy-dev.yml`이 추가되어 있지만, 다음이 있어야 동작합니다.

- `vars.AWS_ACCOUNT_ID` — AWS 계정 ID (저장소 Variables)
- `secrets.AWS_ROLE_TO_ASSUME` — OIDC assume role ARN
- 해당 IAM role의 trust policy에 **gops-frontend 저장소가 `sub` 조건에 포함**되도록 갱신
- `dev` GitHub Environment

`ECR_FRONTEND_REPO`(`alfaka-dev-gops-frontend`)와 EKS 클러스터·네임스페이스는
기존과 동일한 값을 씁니다.

## AWS 계정 ID 제거

저장소가 공개이므로 AWS 계정 ID를 코드에서 걷어냈습니다. 대신 파일 종류에 맞는
치환 방식을 씁니다.

| 대상 | 방식 |
| --- | --- |
| `.github/workflows/*.yml` (두 저장소) | `${{ vars.AWS_ACCOUNT_ID }}` — 저장소 Variables |
| `scripts/aws/*.sh` | `${AWS_ACCOUNT_ID}` 필수 환경변수 (하드코딩 기본값 제거) |
| `docker-compose.yml` | `${S3_BUCKET:-gops-market-data-${AWS_ACCOUNT_ID}-ap-northeast-2-an}` |
| `.env.example` (루트·api-server) | 같은 자리표시자 문자열로 통일 |
| `infra/k8s/**` | `${AWS_ACCOUNT_ID}` 자리표시자 + apply 직전 해석 |
| 문서 | `<aws-account-id>` |
| 테스트 | 예시 계정 ID `123456789012` |

### k8s 매니페스트를 다루는 방식

k8s YAML은 변수 치환을 하지 않으므로, `scripts/aws/resolve-k8s-placeholders.sh`가
**apply 직전에** `${AWS_ACCOUNT_ID}`를 실제 값으로 바꿉니다. 호출 지점은 세 곳입니다.

- `.github/workflows/deploy-dev.yml` — 이미지 태그 갱신 단계 바로 앞
- `scripts/aws/deploy-dev-local.sh` — `prepare_kustomize_overlay()` 시작부
- `systems/agent-orchestration/tests/test_coach_aws_contract.py` — 태그 갱신 계약 테스트

이 순서가 중요합니다. `update-ci-image-tags.sh`는 kustomization의 이미지 이름을
**런타임에 해석된 ECR URL과 문자열로 비교**하므로, 자리표시자가 남아 있으면 태그가
갱신되지 않고 `ci-placeholder` 검사에서 실패합니다.

스크립트는 파일을 제자리에서 수정하므로 **일회용 작업 트리에서만** 실행해야 합니다
(CI 체크아웃, `deploy-dev-local.sh`가 만드는 worktree). `envsubst`에 변수를 한정해
넘기므로 컨테이너 스크립트가 쓰는 `${topic}`, `${CLUSTER_ID}` 등은 건드리지 않습니다.

해석 후 `kubectl kustomize` 렌더가 변경 전과 내용상 동일함을 확인했습니다.

### 남아 있는 노출

강제 푸시로 히스토리를 재작성해도 **GitHub은 기존 커밋을 SHA URL로 한동안 유지**합니다.
완전히 지우려면 GitHub 지원에 GC를 요청해야 하고, 포크가 있다면 그쪽에도 남습니다.
계정 ID는 로테이션할 수 없는 값이므로, 확실한 방어선은 IAM role 신뢰 정책의
`sub`·`aud` 조건을 엄격히 유지하는 것입니다.
