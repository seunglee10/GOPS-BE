#!/usr/bin/env bash
# Backward-compatible wrapper for GOPS ECR repository creation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/ensure-ecr-repositories.sh"
