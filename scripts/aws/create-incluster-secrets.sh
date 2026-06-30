#!/usr/bin/env bash
# Create Kubernetes Secrets for in-cluster Postgres and ClickHouse without committing secret values.
set -euo pipefail

NAMESPACE="${NAMESPACE:-alfaka-market-data}"
DATABASE_NAME="${DATABASE_NAME:-gops}"
DATABASE_USER="${DATABASE_USER:-gops}"
POSTGRES_HOST="${POSTGRES_HOST:-postgres}"
CLICKHOUSE_USER="${CLICKHOUSE_USER:-alfaka}"

kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || kubectl create namespace "${NAMESPACE}" >/dev/null

if kubectl get secret alfaka-order-db-secret -n "${NAMESPACE}" >/dev/null 2>&1; then
  echo "exists: secret/alfaka-order-db-secret"
else
  db_password="$(openssl rand -base64 36 | tr -d '\n')"
  idempotency_secret="$(openssl rand -hex 32)"
  database_url="postgresql://${DATABASE_USER}:${db_password}@${POSTGRES_HOST}:5432/${DATABASE_NAME}"
  kubectl create secret generic alfaka-order-db-secret \
    -n "${NAMESPACE}" \
    --from-literal=DATABASE_PASSWORD="${db_password}" \
    --from-literal=DATABASE_URL="${database_url}" \
    --from-literal=IDEMPOTENCY_HASH_SECRET="${idempotency_secret}" \
    --dry-run=client \
    -o yaml | kubectl apply -f - >/dev/null
  echo "created: secret/alfaka-order-db-secret"
fi

if kubectl get secret alfaka-clickhouse-secret -n "${NAMESPACE}" >/dev/null 2>&1; then
  echo "exists: secret/alfaka-clickhouse-secret"
else
  clickhouse_password="$(openssl rand -base64 36 | tr -d '\n')"
  kubectl create secret generic alfaka-clickhouse-secret \
    -n "${NAMESPACE}" \
    --from-literal=CLICKHOUSE_PASSWORD="${clickhouse_password}" \
    --dry-run=client \
    -o yaml | kubectl apply -f - >/dev/null
  echo "created: secret/alfaka-clickhouse-secret"
fi
