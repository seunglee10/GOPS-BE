#!/usr/bin/env bash
# 역할: Docker가 AWS ECR에 push할 수 있도록 로그인합니다.
# 사용: AWS_ACCOUNT_ID와 AWS_REGION을 실제 값으로 넣고 실행합니다.
# 출력: docker login 세션을 만듭니다.
set -euo pipefail

AWS_REGION="${AWS_REGION:-ap-northeast-2}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:?AWS_ACCOUNT_ID를 넣어주세요}"

aws ecr get-login-password --region "${AWS_REGION}"   | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
