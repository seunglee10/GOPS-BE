#!/usr/bin/env bash
set -euo pipefail

python3 - <<'PY'
from pathlib import Path

root = Path.cwd()
workflow = (root / ".github/workflows/deploy-dev.yml").read_text(encoding="utf-8")
local_deploy = (root / "scripts/aws/deploy-dev-local.sh").read_text(encoding="utf-8")
detector = (root / "scripts/aws/detect-changed-services.sh").read_text(encoding="utf-8")
base = (root / "infra/k8s/base/kustomization.yaml").read_text(encoding="utf-8")

required = {
    "GitHub deploy": (workflow, "run-clickhouse-migrations-job.sh"),
    "local deploy": (local_deploy, "run-clickhouse-migrations-job.sh"),
    "service detector": (detector, "systems/market-data/jobs/clickhouse-migrations/*"),
    "base kustomization": (base, "job-clickhouse-migrations.yaml"),
}
for label, (text, needle) in required.items():
    if needle not in text:
        raise SystemExit(f"{label} is missing ClickHouse migration gate: {needle}")

workflow_gate = workflow.index("run-clickhouse-migrations-job.sh")
workflow_rollout = workflow.index("kubectl apply -k \"$KUSTOMIZE_OVERLAY\"")
if workflow_gate > workflow_rollout:
    raise SystemExit("GitHub ClickHouse migration gate must run before app apply")

local_gate = local_deploy.index("run-clickhouse-migrations-job.sh")
local_rollout = local_deploy.index("deploy_app_workloads()")
if local_gate > local_rollout:
    raise SystemExit("local ClickHouse migration gate must be defined before app rollout")

print("ERD migration deployment contract passed")
PY
