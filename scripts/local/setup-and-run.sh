#!/usr/bin/env bash
# One-shot local bootstrap for GOPS.
# Run this from a Terminal on your Mac (not inside any container):
#   bash scripts/local/setup-and-run.sh
#
# What it does:
#   1. Verifies Docker Desktop is installed and running.
#   2. Creates .env from .env.example if missing.
#   3. Creates/updates the repo-root .venv and installs requirements-dev.txt.
#   4. Stops any already-running GOPS containers (compose down + leftover cleanup).
#   5. Builds and starts the default docker compose stack.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
echo "== GOPS local setup =="
echo "repo root: $REPO_ROOT"

# 1. Docker checks
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker CLI not found. Install Docker Desktop first: https://www.docker.com/products/docker-desktop/" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not running. Start Docker Desktop and re-run this script." >&2
  exit 1
fi

# 2. .env
if [ ! -f .env ]; then
  echo "-- .env not found, copying from .env.example"
  cp .env.example .env
  echo "   NOTE: review .env and fill in real secrets/keys before using non-default features."
fi

# 3. venv (repo-root .venv is the only official local Python env; see AGENTS.md)
PYBIN="python3"
if command -v python3.12 >/dev/null 2>&1; then
  PYBIN="python3.12"
fi
if [ ! -d .venv ]; then
  echo "-- creating venv with: $PYBIN"
  "$PYBIN" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
echo "-- venv python: $(python --version)"

# 4. Tear down any existing GOPS stack (compose-managed + leftover containers)
echo "-- stopping any existing GOPS stack"
docker compose --env-file .env down --remove-orphans || true

LEFTOVER="$(docker ps -a --filter 'name=alfaka-' --filter 'name=gops-frontend' --filter 'name=gops-backend' --format '{{.Names}}' || true)"
if [ -n "$LEFTOVER" ]; then
  echo "-- removing leftover containers:"
  echo "$LEFTOVER"
  # shellcheck disable=SC2086
  docker rm -f $LEFTOVER || true
fi

# 5. Build and start the default stack
echo "-- building and starting docker compose stack"
docker compose --env-file .env up -d --build

echo ""
echo "== Done =="
echo "Frontend: http://localhost:5173"
echo "Backend:  http://localhost:8000/health"
echo "Agents:   http://localhost:8100/health"
echo ""
docker compose ps
echo ""
echo "Tip: live Alpaca ingestion is off by default. Start it with:"
echo "  docker compose --profile alpaca up -d --build alpaca-ingestor"
