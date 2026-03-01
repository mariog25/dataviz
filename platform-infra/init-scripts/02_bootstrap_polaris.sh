#!/usr/bin/env bash
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
INFRA_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)
cd "$INFRA_DIR"

POLARIS_URI=${POLARIS_URI:-http://localhost:8181}
POLARIS_BOOTSTRAP_CREDENTIALS=${POLARIS_BOOTSTRAP_CREDENTIALS:-root=secret}
CATALOG_NAME=${POLARIS_CATALOG_NAME:-lakehouse}
NAMESPACE=${POLARIS_NAMESPACE:-demo}
POLARIS_WAIT_SECONDS=${POLARIS_WAIT_SECONDS:-120}

if [[ "${POLARIS_BOOTSTRAP_CREDENTIALS}" == *"="* ]]; then
  USER_NAME=${POLARIS_BOOTSTRAP_CREDENTIALS%%=*}
  USER_PASS=${POLARIS_BOOTSTRAP_CREDENTIALS#*=}
elif [[ "${POLARIS_BOOTSTRAP_CREDENTIALS}" == *":"* ]]; then
  # backward compatibility with older docs/examples
  USER_NAME=${POLARIS_BOOTSTRAP_CREDENTIALS%%:*}
  USER_PASS=${POLARIS_BOOTSTRAP_CREDENTIALS#*:}
else
  echo "[polaris] ERROR: POLARIS_BOOTSTRAP_CREDENTIALS must use 'user=password' format (example: root=secret)"
  exit 1
fi

echo "[polaris] waiting for Polaris API at ${POLARIS_URI} (timeout=${POLARIS_WAIT_SECONDS}s)"
start_ts=$(date +%s)
until curl -fsS "${POLARIS_URI}/q/health/live" >/dev/null 2>&1; do
  now_ts=$(date +%s)
  if (( now_ts - start_ts > POLARIS_WAIT_SECONDS )); then
    echo "[polaris] ERROR: Polaris did not become reachable on ${POLARIS_URI}"
    echo "[polaris] Tip: run 'docker compose ps polaris' and 'docker compose logs --tail=100 polaris'"
    exit 1
  fi
  sleep 3
done

echo "[polaris] Polaris API reachable"
TOKEN=$(curl -fsS -X POST "${POLARIS_URI}/api/catalog/v1/oauth/tokens" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "grant_type=client_credentials" \
  --data-urlencode "client_id=${USER_NAME}" \
  --data-urlencode "client_secret=${USER_PASS}" \
  | sed -n 's/.*"access_token"[ ]*:[ ]*"\([^"]*\)".*/\1/p' || true)

if [[ -z "${TOKEN}" ]]; then
  echo "[polaris] Could not fetch auth token automatically."
  echo "[polaris] Check credentials in .env (POLARIS_BOOTSTRAP_CREDENTIALS, e.g. root=secret)."
  exit 1
fi

# Idempotent create catalog
curl -fsS -X POST "${POLARIS_URI}/api/management/v1/catalogs" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"${CATALOG_NAME}\",\"type\":\"INTERNAL\",\"properties\":{\"default-base-location\":\"s3://lakehouse/warehouse\"}}" || true

# Namespace endpoint varies by Polaris versions; try management endpoint first, then catalog endpoint.
if ! curl -fsS -X POST "${POLARIS_URI}/api/management/v1/catalogs/${CATALOG_NAME}/namespaces" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "{\"namespace\":[\"${NAMESPACE}\"]}"; then
  curl -fsS -X POST "${POLARIS_URI}/api/catalog/v1/${CATALOG_NAME}/namespaces" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H 'Content-Type: application/json' \
    -d "{\"namespace\":[\"${NAMESPACE}\"]}" || true
fi

echo "[polaris] bootstrap attempted for catalog=${CATALOG_NAME}, namespace=${NAMESPACE}"
