# 역할: AWS S3 또는 로컬 MinIO에 붙는 boto3 S3 client를 생성합니다.
# 사용: S3_ENDPOINT_URL이 있으면 MinIO, 없으면 실제 AWS S3로 연결합니다.
# 설정: AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_ENDPOINT_URL을 봅니다.
import os

import boto3
from botocore.config import Config


def create_s3_client():
    endpoint_url = os.getenv("S3_ENDPOINT_URL") or None
    region_name = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-northeast-2"
    access_key_id = os.getenv("S3_ACCESS_KEY_ID") or None
    secret_access_key = os.getenv("S3_SECRET_ACCESS_KEY") or None
    session_token = os.getenv("S3_SESSION_TOKEN") or None
    credentials = {}
    if access_key_id and secret_access_key:
        credentials = {
            "aws_access_key_id": access_key_id,
            "aws_secret_access_key": secret_access_key,
        }
        if session_token:
            credentials["aws_session_token"] = session_token

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region_name,
        config=Config(s3={"addressing_style": "path"}),
        **credentials,
    )
