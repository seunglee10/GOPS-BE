#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
SYMBOLS="${SYMBOLS:-}"
INTERVALS="${INTERVALS:-1m,5m,10m,1h,4h,1D,1W}"
FORCE="${FORCE:-false}"
JOB_NAME="${JOB_NAME:-chart-geometry-build-$(date +%Y%m%d%H%M%S)}"
IMAGE="$(kubectl -n "$NAMESPACE" get deployment/chart-asset-builder -o jsonpath='{.spec.template.spec.containers[0].image}')"

kubectl apply -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
spec:
  backoffLimit: 2
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        app: chart-geometry-build-manual
    spec:
      restartPolicy: Never
      serviceAccountName: alfaka-market-data-sa
      containers:
        - name: enqueue
          image: ${IMAGE}
          imagePullPolicy: IfNotPresent
          command: ["python", "-u", "/app/systems/agent-orchestration/jobs/chart-asset-schedule/main.py"]
          env:
            - name: CHART_ASSET_SYMBOLS
              value: "${SYMBOLS}"
            - name: CHART_ASSET_INTERVALS
              value: "${INTERVALS}"
            - name: CHART_ASSET_FORCE
              value: "${FORCE}"
          envFrom:
            - configMapRef:
                name: alfaka-market-data-config
            - secretRef:
                name: alfaka-order-db-secret
                optional: false
YAML

echo "created job/${JOB_NAME} in ${NAMESPACE}"
