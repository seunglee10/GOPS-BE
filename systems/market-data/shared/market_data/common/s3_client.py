# 역할: AWS S3 또는 로컬 MinIO에 붙는 boto3 S3 client를 생성합니다.
# 사용: S3_ENDPOINT_URL이 있으면 MinIO, 없으면 실제 AWS S3로 연결합니다.
# 설정: AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_ENDPOINT_URL을 봅니다.
import os

import boto3
from botocore.config import Config


def create_s3_client(operation_timeout_seconds=None):
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

    config_kwargs = {"s3": {"addressing_style": "path"}}
    if operation_timeout_seconds is not None:
        total_timeout = max(1.0, float(operation_timeout_seconds))
        connect_timeout = min(3.0, total_timeout / 4.0)
        config_kwargs.update({
            "connect_timeout": connect_timeout,
            # The request-scoped repair enforces the total wall clock budget.
            # Keep any one socket read short as well so a timed-out background
            # operation releases its worker promptly instead of lingering for
            # the remainder of the whole stage budget.
            "read_timeout": min(10.0, max(1.0, total_timeout - connect_timeout)),
            "retries": {"total_max_attempts": 1, "mode": "standard"},
        })

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region_name,
        config=Config(**config_kwargs),
        **credentials,
    )
