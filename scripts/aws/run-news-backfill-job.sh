#!/usr/bin/env bash
# 역할: EKS에서 Alpaca News backfill Job을 수동으로 한 번 실행합니다.
# 기본은 dry-run입니다. 실제 적재는 NEWS_BACKFILL_DRY_RUN=false를 명시해야 합니다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/aws/lib-gops-images.sh
source "${SCRIPT_DIR}/lib-gops-images.sh"

AWS_REGION="${AWS_REGION:-ap-northeast-2}"
NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
JOB_NAME="${JOB_NAME:-alfaka-news-backfill-manual-$(date +%Y%m%d%H%M%S)}"
NEWS_BACKFILL_DRY_RUN="${NEWS_BACKFILL_DRY_RUN:-true}"
NEWS_BACKFILL_UNIVERSE="${NEWS_BACKFILL_UNIVERSE:-sp500}"
NEWS_BACKFILL_SYMBOLS="${NEWS_BACKFILL_SYMBOLS:-}"
NEWS_BACKFILL_START="${NEWS_BACKFILL_START:-}"
NEWS_BACKFILL_END="${NEWS_BACKFILL_END:-}"
NEWS_BACKFILL_DAYS="${NEWS_BACKFILL_DAYS:-365}"
NEWS_BACKFILL_CHUNK_DAYS="${NEWS_BACKFILL_CHUNK_DAYS:-7}"
NEWS_BACKFILL_MAX_SYMBOLS="${NEWS_BACKFILL_MAX_SYMBOLS:-0}"
NEWS_BACKFILL_SHARD_INDEX="${NEWS_BACKFILL_SHARD_INDEX:-0}"
NEWS_BACKFILL_SHARD_COUNT="${NEWS_BACKFILL_SHARD_COUNT:-1}"
NEWS_BACKFILL_MAX_CHUNKS="${NEWS_BACKFILL_MAX_CHUNKS:-0}"
NEWS_BACKFILL_MAX_PAGES_PER_CHUNK="${NEWS_BACKFILL_MAX_PAGES_PER_CHUNK:-0}"
NEWS_BACKFILL_FORCE="${NEWS_BACKFILL_FORCE:-false}"
NEWS_BACKFILL_INCLUDE_CONTENT="${NEWS_BACKFILL_INCLUDE_CONTENT:-true}"
NEWS_BACKFILL_PUBLISH_RECENT_TO_KAFKA="${NEWS_BACKFILL_PUBLISH_RECENT_TO_KAFKA:-true}"

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

cat > "${tmp_file}" <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
spec:
  backoffLimit: 2
  template:
    metadata:
      labels:
        app: ${JOB_NAME}
    spec:
      restartPolicy: Never
      serviceAccountName: alfaka-market-data-sa
      containers:
        - name: news-backfill
          image: ${resolved_image}
          imagePullPolicy: IfNotPresent
          command: ["python", "-u", "systems/market-data/jobs/news-backfill/main.py"]
          env:
            - name: NEWS_BACKFILL_DRY_RUN
              value: "${NEWS_BACKFILL_DRY_RUN}"
            - name: NEWS_BACKFILL_UNIVERSE
              value: "${NEWS_BACKFILL_UNIVERSE}"
            - name: NEWS_BACKFILL_SYMBOLS
              value: "${NEWS_BACKFILL_SYMBOLS}"
            - name: NEWS_BACKFILL_START
              value: "${NEWS_BACKFILL_START}"
            - name: NEWS_BACKFILL_END
              value: "${NEWS_BACKFILL_END}"
            - name: NEWS_BACKFILL_DAYS
              value: "${NEWS_BACKFILL_DAYS}"
            - name: NEWS_BACKFILL_CHUNK_DAYS
              value: "${NEWS_BACKFILL_CHUNK_DAYS}"
            - name: NEWS_BACKFILL_MAX_SYMBOLS
              value: "${NEWS_BACKFILL_MAX_SYMBOLS}"
            - name: NEWS_BACKFILL_SHARD_INDEX
              value: "${NEWS_BACKFILL_SHARD_INDEX}"
            - name: NEWS_BACKFILL_SHARD_COUNT
              value: "${NEWS_BACKFILL_SHARD_COUNT}"
            - name: NEWS_BACKFILL_MAX_CHUNKS
              value: "${NEWS_BACKFILL_MAX_CHUNKS}"
            - name: NEWS_BACKFILL_MAX_PAGES_PER_CHUNK
              value: "${NEWS_BACKFILL_MAX_PAGES_PER_CHUNK}"
            - name: NEWS_BACKFILL_FORCE
              value: "${NEWS_BACKFILL_FORCE}"
            - name: NEWS_BACKFILL_INCLUDE_CONTENT
              value: "${NEWS_BACKFILL_INCLUDE_CONTENT}"
            - name: NEWS_BACKFILL_PUBLISH_RECENT_TO_KAFKA
              value: "${NEWS_BACKFILL_PUBLISH_RECENT_TO_KAFKA}"
          envFrom:
            - configMapRef:
                name: alfaka-market-data-config
            - secretRef:
                name: alfaka-alpaca-secret
                optional: true
            - secretRef:
                name: alfaka-clickhouse-secret
                optional: true
            - secretRef:
                name: alfaka-openai-secret
                optional: true
YAML

echo "Applying job/${JOB_NAME} in namespace ${NAMESPACE}"
echo "Image: ${resolved_image}"
echo "Dry run: ${NEWS_BACKFILL_DRY_RUN}"
echo "Shard: ${NEWS_BACKFILL_SHARD_INDEX}/${NEWS_BACKFILL_SHARD_COUNT}"
kubectl apply -f "${tmp_file}"
if [[ "${WAIT_FOR_JOB:-true}" != "true" ]]; then
  echo "Started job/${JOB_NAME}; not waiting because WAIT_FOR_JOB=${WAIT_FOR_JOB}."
  echo "Watch logs: kubectl logs -n ${NAMESPACE} job/${JOB_NAME} --all-containers=true -f"
  exit 0
fi
deadline_seconds="${JOB_TIMEOUT_SECONDS:-3600}"
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
