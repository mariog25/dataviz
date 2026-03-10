#!/usr/bin/env bash
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INFRA_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)

cd "$INFRA_DIR"

echo "[minio] creating buckets: ${MINIO_BUCKETS:-lakehouse,warehouse,datahub}"
docker compose --profile init run --rm minio-init

echo "[minio] buckets ready"
