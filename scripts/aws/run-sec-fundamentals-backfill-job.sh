#!/usr/bin/env bash
# 역할: EKS에서 SEC companyfacts fundamentals backfill Job을 수동으로 한 번 실행합니다.
# 기본은 dry-run입니다. 실제 적재는 SEC_FUNDAMENTALS_DRY_RUN=false와 SEC_USER_AGENT를 명시해야 합니다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/aws/lib-gops-images.sh
source "${SCRIPT_DIR}/lib-gops-images.sh"

AWS_REGION="${AWS_REGION:-ap-northeast-2}"
NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
JOB_NAME="${JOB_NAME:-alfaka-sec-fundamentals-backfill-manual-$(date +%Y%m%d%H%M%S)}"
SEC_FUNDAMENTALS_DRY_RUN="${SEC_FUNDAMENTALS_DRY_RUN:-true}"
SEC_FUNDAMENTALS_SOURCE="${SEC_FUNDAMENTALS_SOURCE:-api}"
SEC_COMPANYFACTS_ZIP_URL="${SEC_COMPANYFACTS_ZIP_URL:-https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip}"
SEC_COMPANYFACTS_S3_KEY="${SEC_COMPANYFACTS_S3_KEY:-}"
SEC_FUNDAMENTALS_S3_PREFIX="${SEC_FUNDAMENTALS_S3_PREFIX:-fundamentals/sec/companyfacts}"
SEC_FUNDAMENTALS_SYMBOLS="${SEC_FUNDAMENTALS_SYMBOLS:-}"
SEC_FUNDAMENTALS_MAX_COMPANIES="${SEC_FUNDAMENTALS_MAX_COMPANIES:-0}"
SEC_FUNDAMENTALS_BATCH_SIZE="${SEC_FUNDAMENTALS_BATCH_SIZE:-500}"
SEC_FUNDAMENTALS_LOAD_COMPANYFACTS="${SEC_FUNDAMENTALS_LOAD_COMPANYFACTS:-true}"
SEC_FUNDAMENTALS_LOAD_FRAMES="${SEC_FUNDAMENTALS_LOAD_FRAMES:-true}"
SEC_FUNDAMENTALS_WRITE_FRAME_ROWS="${SEC_FUNDAMENTALS_WRITE_FRAME_ROWS:-true}"
SEC_FUNDAMENTALS_FRAME_CONCEPTS="${SEC_FUNDAMENTALS_FRAME_CONCEPTS:-}"
SEC_FUNDAMENTALS_FRAME_PERIODS="${SEC_FUNDAMENTALS_FRAME_PERIODS:-}"
SEC_FUNDAMENTALS_REDIS_TTL_SECONDS="${SEC_FUNDAMENTALS_REDIS_TTL_SECONDS:-0}"
SEC_FUNDAMENTALS_DOWNLOAD_IN_DRY_RUN="${SEC_FUNDAMENTALS_DOWNLOAD_IN_DRY_RUN:-false}"
SEC_USER_AGENT="${SEC_USER_AGENT:-}"
SEC_USER_AGENT_SECRET_NAME="${SEC_USER_AGENT_SECRET_NAME:-alfaka-sec-fundamentals-secret}"

if [[ "${SEC_FUNDAMENTALS_DRY_RUN}" == "false" && -z "${SEC_USER_AGENT}" && -z "${SEC_USER_AGENT_SECRET_NAME}" ]]; then
  echo "SEC_USER_AGENT or SEC_USER_AGENT_SECRET_NAME is required for real SEC fundamentals ingestion." >&2
  exit 1
fi

if [[ -n "${IMAGE:-}" ]]; then
  resolved_image="${IMAGE}"
else
  resolved_image="$(gops_image_url_for_key market-storage):${IMAGE_TAG}"
fi

tmp_file="$(mktemp)"
cleanup() {
  rm -f "${tmp_file}"
}
trap cleanup EXIT

if [[ -n "${SEC_USER_AGENT}" ]]; then
  sec_user_agent_env_block="            - name: SEC_USER_AGENT
              value: \"${SEC_USER_AGENT}\""
else
  sec_user_agent_env_block="            - name: SEC_USER_AGENT
              valueFrom:
                secretKeyRef:
                  name: ${SEC_USER_AGENT_SECRET_NAME}
                  key: SEC_USER_AGENT"
fi

cat > "${tmp_file}" <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
spec:
  ttlSecondsAfterFinished: 86400
  backoffLimit: 2
  template:
    metadata:
      labels:
        app: ${JOB_NAME}
    spec:
      restartPolicy: Never
      nodeSelector:
        karpenter.sh/nodepool: batch
      tolerations:
        - key: gops.io/dedicated
          operator: Equal
          value: batch
          effect: NoSchedule
      serviceAccountName: alfaka-market-data-sa
      containers:
        - name: sec-fundamentals-backfill
          image: ${resolved_image}
          imagePullPolicy: IfNotPresent
          command: ["python", "-u", "systems/fundamentals/jobs/sec-companyfacts-backfill/main.py"]
          env:
            - name: SEC_FUNDAMENTALS_DRY_RUN
              value: "${SEC_FUNDAMENTALS_DRY_RUN}"
            - name: SEC_FUNDAMENTALS_SOURCE
              value: "${SEC_FUNDAMENTALS_SOURCE}"
            - name: SEC_COMPANYFACTS_ZIP_URL
              value: "${SEC_COMPANYFACTS_ZIP_URL}"
            - name: SEC_COMPANYFACTS_S3_KEY
              value: "${SEC_COMPANYFACTS_S3_KEY}"
            - name: SEC_FUNDAMENTALS_S3_PREFIX
              value: "${SEC_FUNDAMENTALS_S3_PREFIX}"
            - name: SEC_FUNDAMENTALS_SYMBOLS
              value: "${SEC_FUNDAMENTALS_SYMBOLS}"
            - name: SEC_FUNDAMENTALS_MAX_COMPANIES
              value: "${SEC_FUNDAMENTALS_MAX_COMPANIES}"
            - name: SEC_FUNDAMENTALS_BATCH_SIZE
              value: "${SEC_FUNDAMENTALS_BATCH_SIZE}"
            - name: SEC_FUNDAMENTALS_LOAD_COMPANYFACTS
              value: "${SEC_FUNDAMENTALS_LOAD_COMPANYFACTS}"
            - name: SEC_FUNDAMENTALS_LOAD_FRAMES
              value: "${SEC_FUNDAMENTALS_LOAD_FRAMES}"
            - name: SEC_FUNDAMENTALS_WRITE_FRAME_ROWS
              value: "${SEC_FUNDAMENTALS_WRITE_FRAME_ROWS}"
            - name: SEC_FUNDAMENTALS_FRAME_CONCEPTS
              value: "${SEC_FUNDAMENTALS_FRAME_CONCEPTS}"
            - name: SEC_FUNDAMENTALS_FRAME_PERIODS
              value: "${SEC_FUNDAMENTALS_FRAME_PERIODS}"
            - name: SEC_FUNDAMENTALS_REDIS_TTL_SECONDS
              value: "${SEC_FUNDAMENTALS_REDIS_TTL_SECONDS}"
            - name: SEC_FUNDAMENTALS_DOWNLOAD_IN_DRY_RUN
              value: "${SEC_FUNDAMENTALS_DOWNLOAD_IN_DRY_RUN}"
${sec_user_agent_env_block}
          envFrom:
            - configMapRef:
                name: alfaka-market-data-config
            - secretRef:
                name: alfaka-clickhouse-secret
                optional: true
YAML

echo "Applying job/${JOB_NAME} in namespace ${NAMESPACE}"
echo "Image: ${resolved_image}"
echo "Dry run: ${SEC_FUNDAMENTALS_DRY_RUN}"
echo "SEC symbols: ${SEC_FUNDAMENTALS_SYMBOLS:-<universe>}"
kubectl apply -f "${tmp_file}"
if [[ "${WAIT_FOR_JOB:-true}" != "true" ]]; then
  echo "Started job/${JOB_NAME}; not waiting because WAIT_FOR_JOB=${WAIT_FOR_JOB}."
  echo "Watch logs: kubectl logs -n ${NAMESPACE} job/${JOB_NAME} --all-containers=true -f"
  exit 0
fi

deadline_seconds="${JOB_TIMEOUT_SECONDS:-7200}"
started_at="$(date +%s)"
while true; do
  succeeded="$(kubectl get "job/${JOB_NAME}" -n "${NAMESPACE}" -o jsonpath='{.status.succeeded}' 2>/dev/null || true)"
  failed="$(kubectl get "job/${JOB_NAME}" -n "${NAMESPACE}" -o jsonpath='{.status.failed}' 2>/dev/null || true)"
  if [[ "${succeeded:-0}" != "" && "${succeeded:-0}" -gt 0 ]]; then
    break
  fi
  if [[ "${failed:-0}" != "" && "${failed:-0}" -gt 0 ]]; then
    kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}" --all-containers=true || true
    exit 1
  fi
  now="$(date +%s)"
  if (( now - started_at > deadline_seconds )); then
    kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}" --all-containers=true || true
    echo "Timed out waiting for job/${JOB_NAME}" >&2
    exit 1
  fi
  sleep 5
done

if ! kubectl wait --for=condition=complete "job/${JOB_NAME}" -n "${NAMESPACE}" --timeout=30s; then
  kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}" --all-containers=true || true
  exit 1
fi
kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}" --all-containers=true
