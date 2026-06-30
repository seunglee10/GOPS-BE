#!/usr/bin/env sh
set -eu

ZIP_PATH="${1:-${GRAPHDB_DEPLOY_ZIP:-/Users/seunglee/Downloads/nasdaq-fibo-graphdb-deploy.zip}}"
VOLUME_NAME="${GRAPHDB_DOCKER_VOLUME:-nasdaq_fibo_graphdb_data}"
TMP_ROOT="${TMPDIR:-/tmp}"
WORK_DIR="$(mktemp -d "$TMP_ROOT/gops-graphdb-restore.XXXXXX")"

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM

if [ ! -f "$ZIP_PATH" ]; then
  echo "GraphDB deploy zip not found: $ZIP_PATH" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker command is required" >&2
  exit 1
fi

if ! command -v unzip >/dev/null 2>&1; then
  echo "unzip command is required" >&2
  exit 1
fi

unzip -q "$ZIP_PATH" -d "$WORK_DIR"
BACKUP_TGZ="$(find "$WORK_DIR" -name graphdb-volume.tgz -type f | head -n 1)"

if [ -z "$BACKUP_TGZ" ]; then
  echo "graphdb-volume.tgz was not found inside $ZIP_PATH" >&2
  exit 1
fi

BACKUP_DIR="$(dirname "$BACKUP_TGZ")"

docker volume create "$VOLUME_NAME" >/dev/null
docker run --rm \
  -v "$VOLUME_NAME:/volume" \
  -v "$BACKUP_DIR:/backup:ro" \
  alpine sh -c 'tar xzf /backup/graphdb-volume.tgz -C /volume'

echo "GraphDB volume restored: $VOLUME_NAME"
echo "Start GraphDB with: docker compose --profile graphdb up -d graphdb"
