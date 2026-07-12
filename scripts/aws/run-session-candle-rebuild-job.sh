#!/usr/bin/env bash
# 역할: 미국 정규장 1m 원본에서 세션 기준 5m/10m/1h/4h를 재생성해 ClickHouse에 저장합니다.
# 기본은 dry-run입니다. APPLY=true일 때만 live ClickHouse 스키마와 캔들 데이터를 변경합니다.
set -euo pipefail

NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
JOB_NAME="${JOB_NAME:-session-candle-rebuild-$(date +%Y%m%d%H%M%S)}"
SYMBOLS="${SYMBOLS:-}"
INTERVALS="${INTERVALS:-5m,10m,1h,4h}"
LOOKBACK_DAYS="${LOOKBACK_DAYS:-365}"
MAX_SYMBOLS="${MAX_SYMBOLS:-0}"
APPLY="${APPLY:-false}"
WAIT_FOR_JOB="${WAIT_FOR_JOB:-false}"
JOB_TIMEOUT_SECONDS="${JOB_TIMEOUT_SECONDS:-21600}"
BATCH_NODEPOOL="${BATCH_NODEPOOL:-batch-warm}"
IMAGE="${IMAGE:-$(kubectl -n "${NAMESPACE}" get deployment/alfaka-market-processor -o jsonpath='{.spec.template.spec.containers[0].image}')}"

if [[ "${APPLY}" == "true" ]]; then
  kubectl -n "${NAMESPACE}" exec clickhouse-0 -- sh -c '
    clickhouse-client \
      --user "$CLICKHOUSE_USER" \
      --password "$CLICKHOUSE_PASSWORD" \
      --query "ALTER TABLE market_data.chart_candles ADD COLUMN IF NOT EXISTS bucket_policy LowCardinality(String) DEFAULT '\''clock_aligned'\'' AFTER canonical_version"
    clickhouse-client \
      --user "$CLICKHOUSE_USER" \
      --password "$CLICKHOUSE_PASSWORD" \
      --query "ALTER TABLE market_data.chart_candles ADD COLUMN IF NOT EXISTS bucket_policy_key LowCardinality(String) AFTER bucket_policy, MODIFY ORDER BY (symbol, interval, event_time, feed_profile, market_session, bucket_policy_key)"
  '
fi

tmp_file="$(mktemp)"
cleanup() { rm -f "${tmp_file}"; }
trap cleanup EXIT

apply_arg=""
if [[ "${APPLY}" == "true" ]]; then
  apply_arg="            - --apply"
fi
symbols_args=""
if [[ -n "${SYMBOLS}" ]]; then
  symbols_args="            - --symbols
            - \"${SYMBOLS}\""
fi

cat > "${tmp_file}" <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
spec:
  backoffLimit: 2
  ttlSecondsAfterFinished: 604800
  template:
    metadata:
      labels:
        app: session-candle-rebuild
    spec:
      restartPolicy: Never
      serviceAccountName: alfaka-market-data-sa
      nodeSelector:
        karpenter.sh/nodepool: ${BATCH_NODEPOOL}
      tolerations:
        - key: gops.io/dedicated
          operator: Equal
          value: batch
          effect: NoSchedule
      containers:
        - name: rebuild
          image: ${IMAGE}
          imagePullPolicy: IfNotPresent
          command: ["python", "-u", "systems/market-data/jobs/candle-bootstrap/main.py"]
          args:
            - --intervals
            - "${INTERVALS}"
            - --lookback-days
            - "${LOOKBACK_DAYS}"
            - --max-symbols
            - "${MAX_SYMBOLS}"
            - --continue-on-error
${symbols_args}
${apply_arg}
          env:
            - name: CLICKHOUSE_ENSURE_SCHEMA_ON_START
              value: "true"
          envFrom:
            - configMapRef:
                name: alfaka-market-data-config
            - secretRef:
                name: alfaka-clickhouse-secret
                optional: false
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
            limits:
              cpu: "2"
              memory: 2Gi
YAML

kubectl apply -f "${tmp_file}"
echo "created job/${JOB_NAME} in ${NAMESPACE}"
echo "image=${IMAGE} apply=${APPLY} intervals=${INTERVALS} lookbackDays=${LOOKBACK_DAYS} symbols=${SYMBOLS:-<sp500>} nodepool=${BATCH_NODEPOOL}"

if [[ "${WAIT_FOR_JOB}" != "true" ]]; then
  echo "watch: kubectl -n ${NAMESPACE} logs -f job/${JOB_NAME}"
  exit 0
fi

if ! kubectl -n "${NAMESPACE}" wait --for=condition=complete "job/${JOB_NAME}" --timeout="${JOB_TIMEOUT_SECONDS}s"; then
  kubectl -n "${NAMESPACE}" logs "job/${JOB_NAME}" --all-containers=true || true
  exit 1
fi
kubectl -n "${NAMESPACE}" logs "job/${JOB_NAME}" --all-containers=true
