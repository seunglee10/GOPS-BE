#!/usr/bin/env bash
# Verify the deployed analysis worker can write an immutable, non-sensitive S3
# canary through the same IRSA identity and environment as coach snapshots.
set -euo pipefail

K8S_NAMESPACE="${K8S_NAMESPACE:-alfaka-market-data}"
DEPLOYMENT="${AI_COACH_WORKER_DEPLOYMENT:-agent-analysis-worker}"
CONTAINER="${AI_COACH_WORKER_CONTAINER:-agent-analysis-worker}"
EXPECTED_SERVICE_ACCOUNT="${AI_COACH_WORKER_SERVICE_ACCOUNT:-ai-coach-worker-sa}"

actual_service_account="$(
  kubectl get "deployment/${DEPLOYMENT}" \
    -n "${K8S_NAMESPACE}" \
    -o jsonpath='{.spec.template.spec.serviceAccountName}'
)"
if [[ "${actual_service_account}" != "${EXPECTED_SERVICE_ACCOUNT}" ]]; then
  printf 'AI coach snapshot gate failed: deployment/%s uses service account %s, expected %s.\n' \
    "${DEPLOYMENT}" "${actual_service_account:-<empty>}" "${EXPECTED_SERVICE_ACCOUNT}" >&2
  exit 1
fi

kubectl exec -i "deployment/${DEPLOYMENT}" \
  -n "${K8S_NAMESPACE}" \
  -c "${CONTAINER}" \
  -- python -u - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone

import boto3


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"AI coach snapshot gate failed: {name} is empty")
    return value


if os.getenv("AI_COACH_SNAPSHOT_ARCHIVE_ENABLED", "").strip().lower() != "true":
    raise RuntimeError("AI coach snapshot gate failed: archive must be enabled")
if os.getenv("AI_COACH_SNAPSHOT_ARCHIVE_REQUIRED", "").strip().lower() != "true":
    raise RuntimeError("AI coach snapshot gate failed: AWS archive must be fail-closed")

bucket = required_env("AI_COACH_SNAPSHOT_S3_BUCKET")
prefix = required_env("AI_COACH_SNAPSHOT_S3_PREFIX").strip("/")
now = datetime.now(timezone.utc)
canary_id = f"deploy-smoke-{uuid.uuid4()}"
payload = {
    "contractVersion": "coach-snapshot-deploy-smoke.v1",
    "generatedAt": now.isoformat().replace("+00:00", "Z"),
    "kind": "non-sensitive-irsa-write-canary",
    "request": {"analysisId": canary_id},
}
body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
digest = hashlib.sha256(body).hexdigest()
key = f"{prefix}/v1/date={now.date().isoformat()}/{canary_id}.json"

response = boto3.client("s3").put_object(
    Bucket=bucket,
    Key=key,
    Body=body,
    ContentType="application/json",
    Metadata={"sha256": digest, "kind": "deployment-smoke"},
    ServerSideEncryption="AES256",
    IfNoneMatch="*",
)
status = int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
if status != 200:
    raise RuntimeError(f"AI coach snapshot gate failed: S3 PutObject returned HTTP {status}")

json.dump(
    {"archiveStatus": "stored", "bucket": bucket, "key": key, "sha256": digest},
    sys.stdout,
    ensure_ascii=False,
    sort_keys=True,
)
sys.stdout.write("\n")
PY
