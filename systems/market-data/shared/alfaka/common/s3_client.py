# 역할: AWS S3 또는 로컬 MinIO에 붙는 boto3 S3 client를 생성합니다.
# 사용: S3_ENDPOINT_URL이 있으면 MinIO, 없으면 실제 AWS S3로 연결합니다.
# 설정: AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_ENDPOINT_URL을 봅니다.
import os

import boto3
from botocore.config import Config


def create_s3_client():
    endpoint_url = os.getenv("S3_ENDPOINT_URL") or None
    region_name = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-northeast-2"

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region_name,
        config=Config(s3={"addressing_style": "path"}),
    )
