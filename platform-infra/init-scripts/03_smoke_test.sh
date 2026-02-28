#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INFRA_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)

cd "$INFRA_DIR"

echo "[smoke] checking MinIO"
curl -fsS http://localhost:9000/minio/health/live >/dev/null

echo "[smoke] checking Trino"
curl -fsS http://localhost:8080/v1/info >/dev/null

echo "[smoke] checking Iceberg catalog via Trino"
docker compose exec -T trino trino --server http://localhost:8080 --execute "SHOW SCHEMAS FROM iceberg" >/dev/null

echo "[smoke] checking demo table query"
docker compose exec -T trino trino --server http://localhost:8080 --execute "SELECT * FROM iceberg.demo.sample_orders" >/dev/null

echo "[smoke] SUCCESS"
