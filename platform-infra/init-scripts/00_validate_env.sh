#!/usr/bin/env bash
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INFRA_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)
cd "$INFRA_DIR"

if [[ ! -f .env ]]; then
  echo "[env] .env not found. Create it from .env.example first."
  exit 1
fi

VALUE=$(grep -E '^POLARIS_BOOTSTRAP_CREDENTIALS=' .env | head -n1 | cut -d= -f2- || true)
if [[ -z "${VALUE}" ]]; then
  echo "[env] POLARIS_BOOTSTRAP_CREDENTIALS not set in .env"
  exit 1
fi

if [[ "${VALUE}" == *":"* ]]; then
  echo "[env] ERROR: POLARIS_BOOTSTRAP_CREDENTIALS uses ':' -> ${VALUE}"
  echo "[env] Use 'user=password' format, e.g. root=secret"
  exit 1
fi

echo "[env] OK: POLARIS_BOOTSTRAP_CREDENTIALS format looks valid (${VALUE%%=*}=****)"

echo "[env] Effective Compose value:"
docker compose config | sed -n '/POLARIS_BOOTSTRAP_CREDENTIALS/p'
