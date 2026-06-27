# 내가 설정해야 할 것들

## 1. Alpaca 결제 후 로컬 실험

`.env`에서 아래 값을 실제 값으로 바꿉니다.

| 설정 | 내가 넣을 값 | 의미 |
|---|---|---|
| `APCA_API_KEY_ID` | Alpaca API Key ID | 로컬 직접 키 방식 |
| `APCA_API_SECRET_KEY` | Alpaca Secret Key | 로컬 직접 키 방식 |
| `ALPACA_FEED` | `sip` | 유료 SIP feed |
| `ALPACA_SYMBOLS` | `AAPL,TSLA,NVDA` | 받을 종목 |
| `ALPACA_CHANNELS` | `bars,updatedBars,trades` | 받을 데이터 종류 |

실제 연결 실행:

```sh
docker compose --profile alpaca up -d --build
```

로그 확인:

```sh
docker logs -f alfaka-alpaca-ingestor
```

## 2. AWS Secrets Manager 방식

Secret 이름 예시:

```text
alfaka/alpaca/api
```

Secret 값:

```json
{
  "APCA_API_KEY_ID": "실제_Alpaca_API_Key_ID",
  "APCA_API_SECRET_KEY": "실제_Alpaca_Secret_Key"
}
```

`.env` 또는 Kubernetes ConfigMap에는 아래만 넣습니다.

```env
AWS_REGION=ap-northeast-2
ALPACA_SECRET_NAME=alfaka/alpaca/api
```

## 3. AWS/Kubernetes에서 바꿔야 할 값

| 위치 | 바꿀 값 |
|---|---|
| `infra/k8s/base/configmap.yaml` | `YOUR_MSK_BOOTSTRAP_SERVERS` |
| `infra/k8s/base/configmap.yaml` | `YOUR_REDIS_ENDPOINT` |
| `infra/k8s/base/configmap.yaml` | `YOUR_S3_BUCKET` |
| `infra/k8s/base/serviceaccount-irsa.example.yaml` | `YOUR_ACCOUNT_ID`, `YOUR_IRSA_ROLE_NAME` |
| `infra/k8s/base/deployment-*.yaml` | `YOUR_ECR_REPOSITORY/...` |

## 4. 로컬 검증 명령

```sh
docker compose up -d --build
PYTHONPATH=packages python scripts/local/send-sample-market-data.py MSFT
PYTHONPATH=packages python scripts/local/check-redis.py MSFT --interval 1m
PYTHONPATH=packages python scripts/local/check-s3.py MSFT --interval 1m
```

프론트:

```text
http://localhost:5173
```

MinIO Console:

```text
http://localhost:9001
ID/PW: minioadmin / minioadmin
```
