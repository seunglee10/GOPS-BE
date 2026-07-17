#!/usr/bin/env bash
# Read-only AWS/EKS readiness gate for the post-market AI coach.
#
# This script never applies manifests, runs migrations, writes S3 objects, or
# changes ClickHouse/PostgreSQL data. Run it after a rollout to prove the
# deployed pods can read the archive-first coach inputs and reports.
set -euo pipefail

K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
WORKER_DEPLOYMENT="${AI_COACH_WORKER_DEPLOYMENT:-agent-analysis-worker}"
WORKER_CONTAINER="${AI_COACH_WORKER_CONTAINER:-agent-analysis-worker}"
API_DEPLOYMENT="${AI_COACH_API_DEPLOYMENT:-gops-backend}"
API_CONTAINER="${AI_COACH_API_CONTAINER:-gops-backend}"
CONFIG_MAP="${AI_COACH_CONFIG_MAP:-alfaka-market-data-config}"
EXPECTED_WORKER_SERVICE_ACCOUNT="${AI_COACH_WORKER_SERVICE_ACCOUNT:-ai-coach-worker-sa}"
EXPECTED_API_SERVICE_ACCOUNT="${AI_COACH_API_SERVICE_ACCOUNT:-alfaka-market-data-sa}"
EXPECTED_IMAGE_TAG="${AI_COACH_EXPECTED_IMAGE_TAG:-}"
SNAPSHOT_PERMISSION_PROBE_KEY="${AI_COACH_SNAPSHOT_PERMISSION_PROBE_KEY:-}"

fail() {
  printf 'AI coach preflight failed: %s\n' "$*" >&2
  exit 1
}

warn() {
  printf 'AI coach preflight warning: %s\n' "$*" >&2
}

require_equals() {
  local label="$1"
  local actual="$2"
  local expected="$3"
  [[ "${actual}" == "${expected}" ]] || fail "${label} is ${actual:-<empty>}, expected ${expected}."
}

config_value() {
  local key="$1"
  kubectl get configmap "${CONFIG_MAP}" -n "${K8S_NAMESPACE}" -o "jsonpath={.data.${key}}"
}

deployment_service_account() {
  local deployment="$1"
  kubectl get "deployment/${deployment}" -n "${K8S_NAMESPACE}" -o jsonpath='{.spec.template.spec.serviceAccountName}'
}

deployment_image() {
  local deployment="$1"
  local container="$2"
  kubectl get "deployment/${deployment}" -n "${K8S_NAMESPACE}" \
    -o "jsonpath={.spec.template.spec.containers[?(@.name=='${container}')].image}"
}

service_account_role() {
  local service_account="$1"
  kubectl get serviceaccount "${service_account}" -n "${K8S_NAMESPACE}" \
    -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}'
}

simulate_s3_read() {
  local role_arn="$1"
  local resource_arn="$2"
  local label="$3"
  local decision
  decision="$(aws iam simulate-principal-policy \
    --policy-source-arn "${role_arn}" \
    --action-names s3:GetObject \
    --resource-arns "${resource_arn}" \
    --query 'EvaluationResults[0].EvalDecision' \
    --output text)"
  require_equals "${label} S3 GetObject policy" "${decision}" "allowed"
}

find_snapshot_probe_key() {
  if [[ -n "${SNAPSHOT_PERMISSION_PROBE_KEY}" ]]; then
    printf '%s' "${SNAPSHOT_PERMISSION_PROBE_KEY}"
    return
  fi
  aws s3api list-objects-v2 \
    --bucket "${bucket}" \
    --prefix "${snapshot_prefix%/}/v1/" \
    --max-keys 1 \
    --query 'Contents[0].Key' \
    --output text
}

probe_existing_worker_snapshot() {
  local key="$1"
  [[ -n "${key}" && "${key}" != "None" ]] || fail "no existing snapshot probe object; run the explicit canary first."
  kubectl exec "deployment/${WORKER_DEPLOYMENT}" -n "${K8S_NAMESPACE}" -c "${WORKER_CONTAINER}" -- \
    python -c "import boto3; response = boto3.client('s3').get_object(Bucket='${bucket}', Key='${key}'); print('worker snapshot read:', response['ResponseMetadata']['HTTPStatusCode'])"
}

archive_enabled="$(config_value AI_COACH_SNAPSHOT_ARCHIVE_ENABLED)"
archive_required="$(config_value AI_COACH_SNAPSHOT_ARCHIVE_REQUIRED)"
bucket="$(config_value AI_COACH_SNAPSHOT_S3_BUCKET)"
snapshot_prefix="$(config_value AI_COACH_SNAPSHOT_S3_PREFIX)"
input_prefix="$(config_value AI_COACH_INPUT_S3_PREFIX || true)"
report_prefix="$(config_value AI_COACH_REPORT_S3_PREFIX || true)"
input_prefix="${input_prefix:-ai-coach/input/v1}"
report_prefix="${report_prefix:-ai-coach/reports/v1}"

require_equals "AI_COACH_SNAPSHOT_ARCHIVE_ENABLED" "${archive_enabled}" "true"
require_equals "AI_COACH_SNAPSHOT_ARCHIVE_REQUIRED" "${archive_required}" "true"
[[ -n "${bucket}" ]] || fail "AI_COACH_SNAPSHOT_S3_BUCKET is empty."
[[ -n "${snapshot_prefix}" ]] || fail "AI_COACH_SNAPSHOT_S3_PREFIX is empty."

require_equals "worker service account" "$(deployment_service_account "${WORKER_DEPLOYMENT}")" "${EXPECTED_WORKER_SERVICE_ACCOUNT}"
require_equals "API service account" "$(deployment_service_account "${API_DEPLOYMENT}")" "${EXPECTED_API_SERVICE_ACCOUNT}"
kubectl get secret alfaka-clickhouse-secret -n "${K8S_NAMESPACE}" >/dev/null
worker_role="$(service_account_role "${EXPECTED_WORKER_SERVICE_ACCOUNT}")"
api_role="$(service_account_role "${EXPECTED_API_SERVICE_ACCOUNT}")"
[[ -n "${worker_role}" ]] || fail "worker service account has no IRSA role annotation."
[[ -n "${api_role}" ]] || fail "API service account has no IRSA role annotation."

if [[ -n "${EXPECTED_IMAGE_TAG}" ]]; then
  worker_image="$(deployment_image "${WORKER_DEPLOYMENT}" "${WORKER_CONTAINER}")"
  api_image="$(deployment_image "${API_DEPLOYMENT}" "${API_CONTAINER}")"
  [[ "${worker_image}" == *":${EXPECTED_IMAGE_TAG}" ]] || fail "worker image is ${worker_image}, expected tag ${EXPECTED_IMAGE_TAG}."
  [[ "${api_image}" == *":${EXPECTED_IMAGE_TAG}" ]] || fail "API image is ${api_image}, expected tag ${EXPECTED_IMAGE_TAG}."
fi

table_count="$(kubectl exec -n "${K8S_NAMESPACE}" clickhouse-0 -- clickhouse-client --query "SELECT count() FROM system.tables WHERE database = 'market_data' AND name IN ('trade_ticks','chart_candles','symbols','sec_company_tickers','news_articles','sec_financial_facts','sec_derived_metrics','yahoo_earnings_estimates')")"
require_equals "ClickHouse AI coach table count" "${table_count}" "8"

yahoo_rows="$(kubectl exec -n "${K8S_NAMESPACE}" clickhouse-0 -- clickhouse-client --query "SELECT count() FROM market_data.yahoo_earnings_estimates")"
if [[ "${yahoo_rows}" == "0" ]]; then
  warn "yahoo_earnings_estimates has no rows; earnings dates will render as 일정 확인 불가 until the collector succeeds."
fi

migration="$(kubectl exec -n "${K8S_NAMESPACE}" postgres-0 -- sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT filename FROM schema_migrations WHERE filename = '\''0008_alert_proposal_source.sql'\''"')"
require_equals "PostgreSQL alert source migration" "${migration}" "0008_alert_proposal_source.sql"

kubectl get cronjob alfaka-yahoo-estimates-sync -n "${K8S_NAMESPACE}" >/dev/null || warn "Yahoo estimates CronJob is absent; earnings dates can remain unavailable."

# S3 deliberately returns 403 for a missing key when ListBucket is not granted.
# Use IAM simulation for the three exact prefixes, then read a known non-sensitive
# snapshot object through the running worker to prove the IRSA credentials work.
simulate_s3_read "${worker_role}" "arn:aws:s3:::${bucket}/${input_prefix%/}/__permission_probe__/missing.json" "worker input archive"
simulate_s3_read "${worker_role}" "arn:aws:s3:::${bucket}/${report_prefix%/}/__permission_probe__/missing.json" "worker report archive"
simulate_s3_read "${api_role}" "arn:aws:s3:::${bucket}/${report_prefix%/}/__permission_probe__/missing.json" "API report archive"
probe_existing_worker_snapshot "$(find_snapshot_probe_key)"

printf '%s\n' "AI coach preflight passed. This check is read-only; run scripts/aws/verify-ai-coach-snapshot-s3.sh separately after rollout to verify worker PutObject without using production user data."
