import json
import os

from fastapi import APIRouter

router = APIRouter()
_runtime_config_logged = False


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "gops-backend"}


@router.get("/health/config")
def runtime_config() -> dict[str, object]:
    return {
        "status": "ok",
        "aws": {
            "region": os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-northeast-2",
            "accessKeyId": presence(os.getenv("AWS_ACCESS_KEY_ID")),
            "secretAccessKey": presence(os.getenv("AWS_SECRET_ACCESS_KEY")),
            "sessionToken": presence(os.getenv("AWS_SESSION_TOKEN")),
        },
        "s3": {
            "bucket": os.getenv("S3_BUCKET") or "",
            "endpoint": presence(os.getenv("S3_ENDPOINT_URL")),
            "endpointMode": "real-aws" if not os.getenv("S3_ENDPOINT_URL") else "custom-endpoint",
        },
        "alpaca": {
            "localKeyId": presence(os.getenv("APCA_API_KEY_ID")),
            "localSecretKey": presence(os.getenv("APCA_API_SECRET_KEY")),
            "secretName": presence(os.getenv("ALPACA_SECRET_NAME")),
            "credentialSource": alpaca_credential_source(),
        },
    }


def log_runtime_config() -> None:
    global _runtime_config_logged
    if _runtime_config_logged:
        return
    _runtime_config_logged = True
    print(f"GOPS runtime config: {json.dumps(runtime_config(), ensure_ascii=False, sort_keys=True)}", flush=True)


def presence(value: str | None) -> str:
    return "SET" if value else "EMPTY"


def alpaca_credential_source() -> str:
    local_key = os.getenv("APCA_API_KEY_ID")
    local_secret = os.getenv("APCA_API_SECRET_KEY")
    if local_key and local_secret and local_key != "your_key_id" and local_secret != "your_secret_key":
        return "local-env"
    if os.getenv("ALPACA_SECRET_NAME"):
        return "aws-secrets-manager"
    return "missing"
