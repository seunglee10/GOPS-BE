#!/usr/bin/env bash
# 역할: AWS overlay Kubernetes manifest를 EKS에 적용합니다.
# 사용: overlay의 YOUR_* placeholder를 실제 값으로 바꾼 뒤 실행합니다.
# 출력: alfaka-market-data namespace에 Pod/ServiceAccount/ConfigMap이 적용됩니다.
set -euo pipefail

kubectl apply -k infra/k8s/overlays/aws
