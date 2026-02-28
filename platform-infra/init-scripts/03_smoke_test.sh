#!/usr/bin/env bash
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INFRA_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)

cd "$INFRA_DIR"

WAIT_SECONDS=${SMOKE_WAIT_SECONDS:-120}

wait_http() {
  local name="$1" url="$2"
  local start_ts now_ts
  start_ts=$(date +%s)
  echo "[smoke] waiting for ${name}: ${url}"
  until curl -fsS "$url" >/dev/null 2>&1; do
    now_ts=$(date +%s)
    if (( now_ts - start_ts > WAIT_SECONDS )); then
      echo "[smoke] ERROR: ${name} not reachable within ${WAIT_SECONDS}s"
      return 1
    fi
    sleep 3
  done
}

wait_http "MinIO" "http://localhost:9000/minio/health/live"
wait_http "Trino" "http://localhost:8080/v1/info"

echo "[smoke] checking Iceberg catalog via Trino"
if ! docker compose exec -T trino trino --server http://localhost:8080 --execute "SHOW SCHEMAS FROM iceberg"; then
  echo "[smoke] ERROR querying Trino catalog. Recent logs:"
  docker compose logs --tail=80 trino || true
  exit 1
fi

echo "[smoke] checking demo table query"
if ! docker compose exec -T trino trino --server http://localhost:8080 --execute "SELECT * FROM iceberg.demo.sample_orders"; then
  echo "[smoke] ERROR reading demo table. Did you run spark-submit demo writer?"
  echo "[smoke] Command: docker compose exec spark spark-submit /opt/data-workloads/spark_jobs/write_demo_table.py"
  exit 1
fi

echo "[smoke] SUCCESS"
