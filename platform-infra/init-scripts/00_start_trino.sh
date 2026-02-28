#!/usr/bin/env bash
set -euo pipefail

POLARIS_URI="${POLARIS_URI:-http://localhost:8181}"
REALM="${POLARIS_REALM:-default-realm}"
CLIENT_ID="${POLARIS_CLIENT_ID:-root}"
CLIENT_SECRET="${POLARIS_CLIENT_SECRET:-secret}"
SCOPE="${POLARIS_SCOPE:-PRINCIPAL_ROLE:ALL}"

echo "[token] requesting Polaris token..."

TOKEN="$(
  curl -fsS -X POST "${POLARIS_URI}/api/catalog/v1/oauth/tokens" \
    -H "Polaris-Realm: ${REALM}" \
    -u "${CLIENT_ID}:${CLIENT_SECRET}" \
    -d "grant_type=client_credentials" \
    -d "scope=${SCOPE}" \
  | jq -r '.access_token'
)"

if [[ -z "${TOKEN}" || "${TOKEN}" == "null" ]]; then
  echo "ERROR: could not obtain token"
  exit 1
fi

echo "[trino] recreating container with fresh token: ${TOKEN}"

POLARIS_TOKEN="${TOKEN}" docker compose up -d --force-recreate trino

echo "[done]"
