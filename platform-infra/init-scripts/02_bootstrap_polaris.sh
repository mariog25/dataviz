#!/usr/bin/env bash
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
set -euo pipefail

POLARIS_URI=${POLARIS_URI:-http://localhost:8181}
POLARIS_BOOTSTRAP_CREDENTIALS=${POLARIS_BOOTSTRAP_CREDENTIALS:-root:secret}
CATALOG_NAME=${POLARIS_CATALOG_NAME:-local_lakehouse}
NAMESPACE=${POLARIS_NAMESPACE:-demo}

USER_NAME=${POLARIS_BOOTSTRAP_CREDENTIALS%%:*}
USER_PASS=${POLARIS_BOOTSTRAP_CREDENTIALS#*:}

TOKEN=$(curl -fsS -X POST "${POLARIS_URI}/api/catalog/v1/oauth/tokens" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "grant_type=client_credentials" \
  --data-urlencode "client_id=${USER_NAME}" \
  --data-urlencode "client_secret=${USER_PASS}" \
  | sed -n 's/.*"access_token"[ ]*:[ ]*"\([^"]*\)".*/\1/p' || true)

if [[ -z "${TOKEN}" ]]; then
  echo "[polaris] Could not fetch auth token automatically."
  echo "[polaris] Manual fallback: create catalog '${CATALOG_NAME}' and namespace '${NAMESPACE}' in Polaris UI/API."
  exit 0
fi

curl -fsS -X POST "${POLARIS_URI}/api/management/v1/catalogs" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"${CATALOG_NAME}\",\"type\":\"INTERNAL\",\"properties\":{\"default-base-location\":\"s3://lakehouse/warehouse\"}}" || true

curl -fsS -X POST "${POLARIS_URI}/api/catalog/v1/${CATALOG_NAME}/namespaces" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -d "{\"namespace\":[\"${NAMESPACE}\"]}" || true

echo "[polaris] bootstrap attempted for catalog=${CATALOG_NAME}, namespace=${NAMESPACE}"
