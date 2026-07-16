#!/usr/bin/env bash
# 역할: EKS에서 10-K 프로파일 backfill Job을 수동으로 한 번 실행합니다.
# 실제 실행은 기존 SEC User-Agent와 OpenAI ExternalSecret을 참조합니다.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/aws/lib-gops-images.sh
source "${SCRIPT_DIR}/lib-gops-images.sh"

NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
JOB_NAME="${JOB_NAME:-alfaka-ten-k-profile-backfill-manual-$(date +%Y%m%d%H%M%S)}"
TEN_K_PROFILE_DRY_RUN="${TEN_K_PROFILE_DRY_RUN:-true}"
TEN_K_PROFILE_DOWNLOAD_IN_DRY_RUN="${TEN_K_PROFILE_DOWNLOAD_IN_DRY_RUN:-false}"
TEN_K_PROFILE_FORCE="${TEN_K_PROFILE_FORCE:-false}"
TEN_K_PROFILE_SYMBOLS="${TEN_K_PROFILE_SYMBOLS:-}"
TEN_K_PROFILE_MAX_COMPANIES="${TEN_K_PROFILE_MAX_COMPANIES:-0}"
TEN_K_PROFILE_S3_PREFIX="${TEN_K_PROFILE_S3_PREFIX:-fundamentals/sec/10k-profiles}"
TEN_K_PROFILE_MODEL="${TEN_K_PROFILE_MODEL:-gpt-5.2}"
SEC_USER_AGENT="${SEC_USER_AGENT:-}"
SEC_USER_AGENT_SECRET_NAME="${SEC_USER_AGENT_SECRET_NAME:-alfaka-sec-fundamentals-secret}"
OPENAI_SECRET_NAME="${OPENAI_SECRET_NAME:-alfaka-openai-secret}"

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
  activeDeadlineSeconds: 14400
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
        - name: ten-k-profile-backfill
          image: ${resolved_image}
          imagePullPolicy: IfNotPresent
          command: ["python", "-u", "systems/fundamentals/jobs/10k-profile-backfill/main.py"]
          env:
            - name: TEN_K_PROFILE_DRY_RUN
              value: "${TEN_K_PROFILE_DRY_RUN}"
            - name: TEN_K_PROFILE_DOWNLOAD_IN_DRY_RUN
              value: "${TEN_K_PROFILE_DOWNLOAD_IN_DRY_RUN}"
            - name: TEN_K_PROFILE_FORCE
              value: "${TEN_K_PROFILE_FORCE}"
            - name: TEN_K_PROFILE_SYMBOLS
              value: "${TEN_K_PROFILE_SYMBOLS}"
            - name: TEN_K_PROFILE_MAX_COMPANIES
              value: "${TEN_K_PROFILE_MAX_COMPANIES}"
            - name: TEN_K_PROFILE_S3_PREFIX
              value: "${TEN_K_PROFILE_S3_PREFIX}"
            - name: TEN_K_PROFILE_MODEL
              value: "${TEN_K_PROFILE_MODEL}"
            - name: OPENAI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: ${OPENAI_SECRET_NAME}
                  key: OPENAI_API_KEY
${sec_user_agent_env_block}
          envFrom:
            - configMapRef:
                name: alfaka-market-data-config
          resources:
            requests:
              cpu: "1"
              memory: 1Gi
            limits:
              cpu: "2"
              memory: 3Gi
YAML

echo "Applying job/${JOB_NAME} in namespace ${NAMESPACE}"
echo "Image: ${resolved_image}"
echo "Dry run: ${TEN_K_PROFILE_DRY_RUN}"
echo "10-K symbols: ${TEN_K_PROFILE_SYMBOLS:-<universe>}"
kubectl apply -f "${tmp_file}"

if [[ "${WAIT_FOR_JOB:-true}" != "true" ]]; then
  echo "Started job/${JOB_NAME}; not waiting because WAIT_FOR_JOB=${WAIT_FOR_JOB}."
  echo "Watch logs: kubectl logs -n ${NAMESPACE} job/${JOB_NAME} --all-containers=true -f"
  exit 0
fi

deadline_seconds="${JOB_TIMEOUT_SECONDS:-14400}"
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

kubectl wait --for=condition=complete "job/${JOB_NAME}" -n "${NAMESPACE}" --timeout=30s
kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}" --all-containers=true
