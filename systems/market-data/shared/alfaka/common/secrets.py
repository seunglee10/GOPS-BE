# 역할: Alpaca API 키를 로컬 .env 또는 AWS Secrets Manager에서 읽습니다.
# 사용: 로컬 실험은 .env, AWS/EKS 운영은 ALPACA_SECRET_NAME + IAM 권한을 권장합니다.
# Secret: {"APCA_API_KEY_ID":"...", "APCA_API_SECRET_KEY":"..."}
import json
import os
import sys

import boto3


def load_alpaca_credentials():
    local_key = os.getenv("APCA_API_KEY_ID")
    local_secret = os.getenv("APCA_API_SECRET_KEY")

    if local_key and local_secret and local_key != "your_key_id" and local_secret != "your_secret_key":
        return local_key, local_secret

    secret_name = os.getenv("ALPACA_SECRET_NAME")
    aws_region = os.getenv("AWS_REGION", "ap-northeast-2")

    if not secret_name:
        return None, None

    try:
        client = boto3.client("secretsmanager", region_name=aws_region)
        response = client.get_secret_value(SecretId=secret_name)
        secret_text = response.get("SecretString")
        if not secret_text:
            raise RuntimeError("SecretString이 비어 있습니다.")

        secret_payload = json.loads(secret_text)
        key = secret_payload.get("APCA_API_KEY_ID") or secret_payload.get("key")
        secret = secret_payload.get("APCA_API_SECRET_KEY") or secret_payload.get("secret")
        return key, secret
    except Exception as exc:
        print(f"AWS Secrets Manager에서 Alpaca 키를 읽지 못했습니다: {exc}", file=sys.stderr)
        return None, None
