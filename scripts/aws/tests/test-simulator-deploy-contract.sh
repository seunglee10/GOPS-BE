#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
RENDERED_MANIFEST="$(mktemp)"
trap 'rm -f "${RENDERED_MANIFEST}"' EXIT

cd "${REPO_ROOT}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
fi
kubectl kustomize infra/k8s/overlays/aws-incluster-app-ci > "${RENDERED_MANIFEST}"

"${PYTHON_BIN}" - "${RENDERED_MANIFEST}" <<'PY'
from pathlib import Path
import sys

import yaml


repo_root = Path.cwd()
documents = list(yaml.safe_load_all(Path(sys.argv[1]).read_text(encoding="utf-8")))
simulator = next(
    (
        document
        for document in documents
        if isinstance(document, dict)
        and document.get("kind") == "Deployment"
        and (document.get("metadata") or {}).get("name") == "gops-simulator"
    ),
    None,
)
if simulator is None:
    raise SystemExit("rendered app overlay does not contain Deployment/gops-simulator")
if (simulator.get("spec") or {}).get("replicas") != 1:
    raise SystemExit("Deployment/gops-simulator must render with replicas=1")

local_deploy = (repo_root / "scripts/aws/deploy-dev-local.sh").read_text(encoding="utf-8")
if 'kubectl rollout status deployment/gops-simulator' not in local_deploy:
    raise SystemExit("local deploy must wait for Deployment/gops-simulator")

workflow = (repo_root / ".github/workflows/deploy-dev.yml").read_text(encoding="utf-8")
if 'kubectl rollout status deployment/gops-simulator' not in workflow:
    raise SystemExit("GitHub deploy must wait for Deployment/gops-simulator")

for script_name in ("start-dev-simulator.sh", "stop-dev-simulator.sh"):
    script = (repo_root / f"scripts/aws/{script_name}").read_text(encoding="utf-8")
    if "kubectl scale deployment/gops-simulator" in script:
        raise SystemExit(f"{script_name} must not override the declarative simulator replica count")
    if "kubectl set env deployment/gops-backend" in script:
        raise SystemExit(f"{script_name} must not override the declarative backend simulator URL")

dockerfile = (repo_root / "infra/docker/Dockerfile.gops-simulator").read_text(encoding="utf-8")
if "COPY systems/market-data/shared /app/market-data-shared" not in dockerfile:
    raise SystemExit("simulator image must include the shared order-flow implementation")
if "PYTHONPATH=/app:/app/market-data-shared" not in dockerfile:
    raise SystemExit("simulator image must make the shared alfaka namespace importable")

detector = (repo_root / "scripts/aws/detect-changed-services.sh").read_text(encoding="utf-8")
shared_case = detector.split("systems/market-data/shared/*)", 1)[1].split(";;", 1)[0]
if "add_service simulator" not in shared_case:
    raise SystemExit("market-data shared changes must rebuild the simulator image")

print("simulator deploy contract passed")
PY
