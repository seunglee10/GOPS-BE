# AWS Docker Handoff Spec

작성일: 2026-06-28

## 목적

AWS 담당자와 개발자가 같은 기준으로 Docker 로컬 환경, AWS 리소스, EKS 이전 지점을 확인하기 위한 공유 명세서입니다.

현재 개발 환경은 로컬 Docker Compose에서 Kafka, Redis, ClickHouse, Backend, Frontend, Worker를 실행하고, S3와 Secrets Manager는 실제 AWS 리소스를 사용합니다. EKS 배포 시에는 로컬 장기 AWS access key 대신 IRSA를 사용해야 합니다.

## 작업 위치

```sh
cd "/Users/heejunkim/Desktop/kim hee jun/gops"
```

주요 설정 파일:

| 파일 | 역할 |
| --- | --- |
| `.env` | 로컬 전용 비밀/접속 값. Git에 올리지 않습니다. |
| `.env.example` | 공유 가능한 환경 변수 예시. |
| `docker-compose.yml` | 로컬 Docker 실행 기준. |
| `infra/aws/terraform` | ECR, S3/Secret reference, IRSA IAM role/policy 접착 리소스. |
| `infra/k8s/base` | Kubernetes 공통 manifest. |
| `infra/k8s/overlays/aws` | AWS/EKS 배포용 kustomize overlay. |

## 현재 AWS 리소스

| 항목 | 값 | 상태 |
| --- | --- | --- |
| AWS account ID | `<aws-account-id>` | 확인됨 |
| Region | `ap-northeast-2` | 확인됨 |
| 현재 확인 IAM ARN | `arn:aws:iam::<aws-account-id>:user/heejun` | 확인됨 |
| S3 bucket | `gops-market-data-<aws-account-id>-ap-northeast-2-an` | 존재, `ap-northeast-2` |
| Secrets Manager | `dev/alpaca` | 존재 |
| Secret ARN | `arn:aws:secretsmanager:ap-northeast-2:<aws-account-id>:secret:dev/alpaca-Bg2lkp` | 값 출력 금지 |
| EKS cluster | `gops-eks-cluster` | `ACTIVE` |
| EKS version | `1.36` | 확인됨 |
| EKS endpoint | `https://CBF812F3CD9E9F92E09BA4C33C1A1DB1.gr7.ap-northeast-2.eks.amazonaws.com` | 확인됨 |
| EKS OIDC issuer | `https://oidc.eks.ap-northeast-2.amazonaws.com/id/CBF812F3CD9E9F92E09BA4C33C1A1DB1` | IRSA에 필요 |
| ECR repositories | overlay 3개 + service별 repository | 생성 및 push 완료 |

Secrets Manager `dev/alpaca`의 값은 아래 JSON 형태여야 합니다.

```json
{"APCA_API_KEY_ID":"...","APCA_API_SECRET_KEY":"..."}
```

값 자체는 문서, 로그, PR, 이슈에 남기지 않습니다.

## ECR Repository

현재 Kubernetes overlay는 아래 ECR URI를 사용하도록 되어 있습니다. Repository는 생성됐고, `latest`와 `cf8848f` 태그 push까지 완료됐습니다.

| 용도 | Repository | 예상 URI |
| --- | --- | --- |
| market-data worker 공용 | `alfaka-dev-market-data-worker` | `<aws-account-id>.dkr.ecr.ap-northeast-2.amazonaws.com/alfaka-dev-market-data-worker` |
| backend | `alfaka-dev-gops-backend` | `<aws-account-id>.dkr.ecr.ap-northeast-2.amazonaws.com/alfaka-dev-gops-backend` |
| frontend | `alfaka-dev-gops-frontend` | `<aws-account-id>.dkr.ecr.ap-northeast-2.amazonaws.com/alfaka-dev-gops-frontend` |

서비스별 확인용 repository도 함께 push했습니다.

| 로컬 이미지 | ECR repository | Tags |
| --- | --- | --- |
| `gops-alpaca-ingestor` | `gops/alpaca-connector` | `latest`, `cf8848f` |
| `gops-local-stream-processor` | `gops/flink-stream-processor` | `latest`, `cf8848f` |
| `gops-s3-sink` | `gops/s3-store` | `latest`, `cf8848f` |
| `gops-clickhouse-loader` | `gops/clickhouse-store` | `latest`, `cf8848f` |
| `gops-backfill-worker` | `gops/backfill-worker` | `latest`, `cf8848f` |
| `gops-symbol-registry-sync` | `gops/symbol-registry-sync` | `latest`, `cf8848f` |
| `gops-gops-backend` | `gops/api-websocket` | `latest`, `cf8848f` |
| `gops-gops-frontend` | `gops/gops-frontend` | `latest`, `cf8848f` |

주의: 현재 push된 GOPS custom image는 로컬 Docker 기준 `linux/arm64`입니다. EKS node가 x86_64/amd64이면 `linux/amd64` 또는 multi-arch image로 다시 빌드해서 같은 tag로 push해야 합니다.

확인 명령:

```sh
aws ecr describe-repositories \
  --repository-names alfaka-dev-market-data-worker alfaka-dev-gops-backend alfaka-dev-gops-frontend \
  --region ap-northeast-2
```

이미지 태그 확인:

```sh
aws ecr describe-images \
  --repository-name alfaka-dev-market-data-worker \
  --image-ids imageTag=latest imageTag=cf8848f \
  --region ap-northeast-2
```

## Docker 실행 방식

현재 실제 AWS S3와 Secrets Manager를 쓰는 로컬 개발 실행 방식:

```sh
docker compose --env-file .env --profile alpaca up -d --build
```

현재 로컬 S3(MinIO)는 기본 실행에서 제외되어 있습니다. MinIO를 써야 할 때만 `local-s3` profile을 추가합니다.

```sh
docker compose --env-file .env --profile local-s3 --profile alpaca up -d --build
```

현재 기준은 실제 S3 사용입니다.

| 변수 | 현재 기준 |
| --- | --- |
| `S3_BUCKET` | `gops-market-data-<aws-account-id>-ap-northeast-2-an` |
| `S3_ENDPOINT_URL` | 비움 |
| `DOCKER_S3_ENDPOINT_URL` | 비움 |
| `ALPACA_SECRET_NAME` | `dev/alpaca` |
| `AWS_SESSION_TOKEN` | 비움 |

로컬에서는 `.env`의 `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`로 AWS에 접근합니다. EKS에서는 장기 key를 넣지 않고 IRSA service account role로 대체해야 합니다.

## Docker Image 목록

아래 표는 `docker compose --env-file .env --profile alpaca images` 기준입니다. Image ID와 size는 rebuild 시 바뀔 수 있으므로 담당자는 확인 명령으로 다시 검증합니다.

| Compose container | Repository:tag | Platform | Image ID | Size | Build/Source |
| --- | --- | --- | --- | --- | --- |
| `alfaka-alpaca-ingestor` | `gops-alpaca-ingestor:latest` | `linux/arm64` | `a9375e13e4b3` | `112MB` | `infra/docker/Dockerfile.worker` |
| `alfaka-backfill-worker` | `gops-backfill-worker:latest` | `linux/arm64` | `880675a5af42` | `112MB` | `infra/docker/Dockerfile.worker` |
| `alfaka-clickhouse` | `clickhouse/clickhouse-server:24.12-alpine` | `linux/amd64` | `cd450891db46` | `155MB` | public image |
| `alfaka-clickhouse-loader` | `gops-clickhouse-loader:latest` | `linux/arm64` | `cb8c86a57bd1` | `112MB` | `infra/docker/Dockerfile.worker` |
| `alfaka-gops-backend` | `gops-gops-backend:latest` | `linux/arm64` | `80d2f2a66080` | `123MB` | `infra/docker/Dockerfile.gops-backend` |
| `alfaka-gops-frontend` | `gops-gops-frontend:latest` | `linux/arm64` | `72936353422c` | `95MB` | `infra/docker/Dockerfile.gops-frontend` |
| `alfaka-kafka` | `apache/kafka:latest` | `linux/amd64` | `77e3df905404` | `239MB` | public image |
| `alfaka-kafka-init` | `apache/kafka:latest` | `linux/amd64` | `77e3df905404` | `239MB` | public image |
| `alfaka-local-stream-processor` | `gops-local-stream-processor:latest` | `linux/arm64` | `980e32a229d7` | `112MB` | `infra/docker/Dockerfile.worker` |
| `alfaka-redis` | `redis:7-alpine` | `linux/amd64` | `6ab0b6e73817` | `16.3MB` | public image |
| `alfaka-s3-sink` | `gops-s3-sink:latest` | `linux/arm64` | `90187a88284c` | `112MB` | `infra/docker/Dockerfile.worker` |
| `alfaka-symbol-registry-sync` | `gops-symbol-registry-sync:latest` | `linux/arm64` | `8d9d89d2062a` | `112MB` | `infra/docker/Dockerfile.worker` |

확인 명령:

```sh
docker compose --env-file .env --profile alpaca images
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}'
```

## Docker Container 목록

아래 표는 `docker compose --env-file .env --profile alpaca ps` 기준입니다.

| Container | Service | Image | Port | 현재 상태 | 역할 |
| --- | --- | --- | --- | --- | --- |
| `alfaka-kafka` | `kafka` | `apache/kafka:latest` | `9092:9092` | Up, healthy | 로컬 Kafka/MSK 대체 |
| `alfaka-kafka-init` | `kafka-init` | `apache/kafka:latest` | 없음 | 일회성 | topic 생성 |
| `alfaka-redis` | `redis` | `redis:7-alpine` | `6379:6379` | Up, healthy | cache, queue, symbol metadata |
| `alfaka-clickhouse` | `clickhouse` | `clickhouse/clickhouse-server:24.12-alpine` | `8123:8123`, `9002:9000` | Up, healthy | chart candle 저장/조회 |
| `alfaka-alpaca-ingestor` | `alpaca-ingestor` | `gops-alpaca-ingestor:latest` | 없음 | Up | Alpaca stream 수신, Kafka publish |
| `alfaka-local-stream-processor` | `local-stream-processor` | `gops-local-stream-processor:latest` | 없음 | Up | 로컬 Flink 대체 stream processor |
| `alfaka-s3-sink` | `s3-sink` | `gops-s3-sink:latest` | 없음 | Up | processed Kafka topic을 S3에 저장 |
| `alfaka-clickhouse-loader` | `clickhouse-loader` | `gops-clickhouse-loader:latest` | 없음 | Up | processed Kafka topic을 ClickHouse에 적재 |
| `alfaka-symbol-registry-sync` | `symbol-registry-sync` | `gops-symbol-registry-sync:latest` | 없음 | 일회성 | Alpaca asset 목록을 ClickHouse/Redis에 동기화 |
| `alfaka-backfill-worker` | `backfill-worker` | `gops-backfill-worker:latest` | 없음 | Up | Alpaca historical backfill, S3/ClickHouse 적재 |
| `alfaka-gops-backend` | `gops-backend` | `gops-gops-backend:latest` | `8000:8000` | Up | FastAPI chart/backfill API |
| `alfaka-gops-frontend` | `gops-frontend` | `gops-gops-frontend:latest` | `5173:5173` | Up | Web frontend |

확인 명령:

```sh
docker compose --env-file .env --profile alpaca ps
docker compose --env-file .env --profile alpaca logs --tail=100 gops-backend
docker compose --env-file .env --profile alpaca logs --tail=100 backfill-worker
docker compose --env-file .env --profile alpaca logs --tail=100 alpaca-ingestor
```

## Network와 Volume

| 이름 | 역할 |
| --- | --- |
| `alfaka-app-net` | frontend/backend/worker application 통신 |
| `alfaka-streaming-net` | Kafka stream 통신 |
| `alfaka-data-net` | Redis/ClickHouse/S3 저장소 통신 |
| `redis_data` | Redis appendonly data |
| `clickhouse_data` | ClickHouse local data |
| `minio_data` | local-s3 profile에서만 사용 |

확인 명령:

```sh
docker network ls | grep alfaka
docker volume ls | grep gops
```

## 로컬 포트

| Port | Service | 확인 URL/명령 |
| --- | --- | --- |
| `5173` | Frontend | `http://localhost:5173` |
| `8000` | Backend API | `curl -fsS http://localhost:8000/health` |
| `8123` | ClickHouse HTTP | `curl -fsS 'http://localhost:8123/?query=SELECT%201'` |
| `9002` | ClickHouse native | `clickhouse-client --port 9002` |
| `9092` | Kafka external listener | `kafka-topics --bootstrap-server localhost:9092 --list` |
| `6379` | Redis | `redis-cli -p 6379 ping` |

## 데이터 흐름

```text
Alpaca stream/API
  -> alpaca-ingestor / backfill-worker
  -> Kafka raw/processed topics
  -> local-stream-processor
  -> Redis live cache
  -> s3-sink -> S3 bucket
  -> clickhouse-loader / backfill-worker -> ClickHouse
  -> gops-backend -> gops-frontend
```

현재 백필은 `BACKFILL_EXECUTION_MODE=queue`입니다. Frontend 또는 API가 `/api/charts/backfill`을 호출하면 Redis queue에 요청이 들어가고 `backfill-worker`가 처리합니다.

## 현재 주요 데이터 검증 결과

`/api/charts/candles?interval=1m&limit=390` 기준으로 아래 종목은 모두 `backfill=succeeded`, `renderable=True`, `state=complete`입니다.

| Symbol | Stored candles |
| --- | ---: |
| `NVDA` | `236331` |
| `AMD` | `205629` |
| `AVGO` | `185702` |
| `TSM` | `180494` |
| `ASML` | `129592` |
| `AMAT` | `120360` |
| `MU` | `203075` |

ClickHouse 중복 timestamp 검증은 주요 종목 모두 `duplicate_times=0`입니다.

검증 명령:

```sh
python3 - <<'PY'
import json
import urllib.request

symbols = ["NVDA", "AMD", "AVGO", "TSM", "ASML", "AMAT", "MU"]
for symbol in symbols:
    url = f"http://localhost:8000/api/charts/candles?symbol={symbol}&interval=1m&limit=390"
    with urllib.request.urlopen(url, timeout=10) as res:
        data = json.load(res)
    coverage = data.get("coverage") or {}
    print(symbol, data.get("storedCandleCount"), data.get("backfillStatus"), coverage.get("renderable"), coverage.get("state"))
PY
```

## AWS 접근 검증 명령

Secret 값은 절대 출력하지 않습니다.

```sh
aws sts get-caller-identity

aws s3api get-bucket-location \
  --bucket gops-market-data-<aws-account-id>-ap-northeast-2-an

aws secretsmanager describe-secret \
  --secret-id dev/alpaca \
  --region ap-northeast-2

aws eks describe-cluster \
  --name gops-eks-cluster \
  --region ap-northeast-2 \
  --query '{name:cluster.name,status:cluster.status,version:cluster.version,oidc:cluster.identity.oidc.issuer}'
```

로컬 Docker 컨테이너가 장기 AWS key를 쓰고 임시 token을 쓰지 않는지 확인:

```sh
docker compose --env-file .env --profile alpaca exec -T backfill-worker sh -lc '
printf "AWS_ACCESS_KEY_ID=%s\nAWS_SECRET_ACCESS_KEY=%s\nAWS_SESSION_TOKEN=%s\n" \
  "$([ -n "$AWS_ACCESS_KEY_ID" ] && echo present || echo missing)" \
  "$([ -n "$AWS_SECRET_ACCESS_KEY" ] && echo present || echo missing)" \
  "$([ -n "$AWS_SESSION_TOKEN" ] && echo present || echo empty)"
'
```

기대값:

```text
AWS_ACCESS_KEY_ID=present
AWS_SECRET_ACCESS_KEY=present
AWS_SESSION_TOKEN=empty
```

## EKS 전환 시 매핑

| 로컬 Docker | EKS/AWS 대응 | 비고 |
| --- | --- | --- |
| `apache/kafka` | MSK 또는 초기에는 로컬/EC2 Kafka | 비용 때문에 초반은 로컬 Kafka 유지 가능 |
| `local-stream-processor` | Flink/MSF 또는 임시 worker deployment | 운영 전까지는 로컬 processor 역할 유지 |
| `redis:7-alpine` | ElastiCache Redis/Valkey | 비용 고려 필요 |
| `clickhouse/clickhouse-server` | ClickHouse 서버/Cloud/EC2 | endpoint를 `CLICKHOUSE_HTTP_URL`에 반영 |
| `.env` AWS keys | IRSA role | EKS에는 access key를 넣지 않습니다 |
| 실제 S3 bucket | 동일 bucket | `S3_BUCKET` 유지 |
| `dev/alpaca` secret | Secrets Manager + IRSA access | pod가 읽을 수 있어야 함 |

Kubernetes overlay에서 AWS 담당자가 바꿔야 할 placeholder:

| Placeholder | 넣을 값 |
| --- | --- |
| `YOUR_MSK_BOOTSTRAP_SERVERS` | MSK bootstrap broker 문자열 |
| `YOUR_REDIS_ENDPOINT` | Redis/Valkey endpoint host |
| `YOUR_CLICKHOUSE_ENDPOINT` | ClickHouse HTTP endpoint host |
| `YOUR_IRSA_ROLE_ARN` | Terraform output의 IRSA role ARN |
| ECR image tag | `latest` 대신 배포 commit SHA 권장 |

## AWS 담당자 작업 체크리스트

1. ECR repository와 image tag `latest`, `cf8848f`는 현재 push 완료 상태입니다.
2. EKS OIDC provider ARN 확인.
3. IRSA role 생성 후 service account annotation에 role ARN 반영.
4. `dev/alpaca` secret에 pod role이 `secretsmanager:GetSecretValue` 가능하도록 IAM policy 확인.
5. S3 bucket에 pod role이 `s3:PutObject`, `s3:GetObject`, `s3:ListBucket` 가능하도록 IAM policy 확인.
6. MSK/Redis/ClickHouse endpoint를 `infra/k8s/overlays/aws/configmap-aws-patch.yaml`에 반영.
7. `kubectl kustomize infra/k8s/overlays/aws`로 manifest 렌더링 확인.
8. `kubectl apply -k infra/k8s/overlays/aws` 적용.
9. Pod log에서 Alpaca secret read, S3 write, ClickHouse read/write 성공 확인.

## ECR Push 예시

이미 push 완료됐습니다. 이미지를 다시 빌드한 뒤 재업로드할 때만 아래 흐름을 사용합니다.

```sh
aws ecr get-login-password --region ap-northeast-2 \
  | docker login --username AWS --password-stdin <aws-account-id>.dkr.ecr.ap-northeast-2.amazonaws.com

docker tag gops-s3-sink:latest \
  <aws-account-id>.dkr.ecr.ap-northeast-2.amazonaws.com/alfaka-dev-market-data-worker:latest

docker tag gops-gops-backend:latest \
  <aws-account-id>.dkr.ecr.ap-northeast-2.amazonaws.com/alfaka-dev-gops-backend:latest

docker tag gops-gops-frontend:latest \
  <aws-account-id>.dkr.ecr.ap-northeast-2.amazonaws.com/alfaka-dev-gops-frontend:latest

docker push <aws-account-id>.dkr.ecr.ap-northeast-2.amazonaws.com/alfaka-dev-market-data-worker:latest
docker push <aws-account-id>.dkr.ecr.ap-northeast-2.amazonaws.com/alfaka-dev-gops-backend:latest
docker push <aws-account-id>.dkr.ecr.ap-northeast-2.amazonaws.com/alfaka-dev-gops-frontend:latest
```

주의: worker 계열 이미지는 같은 Dockerfile이지만 로컬 Compose에서는 서비스별 repository name으로 build되어 있습니다. EKS overlay는 worker 공용 ECR repository 하나를 사용합니다.

## 운영 전 결정 필요 사항

| 항목 | 선택지 | 현재 권장 |
| --- | --- | --- |
| Kafka | MSK / EC2 Kafka / 로컬 유지 | 예산 때문에 개발 초반은 로컬 Kafka, EKS 전환 시 MSK 재검토 |
| Redis | ElastiCache / 자체 Redis on EC2/EKS | 비용 최소화면 작은 EC2 또는 EKS Redis, 안정화 후 ElastiCache |
| ClickHouse | ClickHouse Cloud / EC2 단일 노드 / EKS StatefulSet | 예산상 EC2 단일 노드 또는 현재 로컬 유지 후 전환 |
| Flink | Managed Service for Apache Flink / EKS job / 현재 local processor | 초반은 현재 local-stream-processor 유지 |
| Image tag | `latest` / commit SHA | 협업 배포는 commit SHA 권장 |
| AWS credential | access key / IRSA | 로컬은 access key, EKS는 IRSA 필수 |

## 금지 사항

- `.env`, AWS access key CSV, secret 값을 Git에 커밋하지 않습니다.
- `AWS_SESSION_TOKEN`을 장기 실행 Docker 환경에 남기지 않습니다. 만료 후 다시 `ExpiredToken`이 발생합니다.
- EKS pod에 `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`를 직접 넣지 않습니다.
- Secret 값을 `kubectl describe`, Slack, 문서, 로그에 붙여넣지 않습니다.
