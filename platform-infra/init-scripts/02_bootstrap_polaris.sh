#!/usr/bin/env bash
set -euo pipefail

POLARIS_URI=${POLARIS_URI:-http://localhost:8181}
POLARIS_BOOTSTRAP_CREDENTIALS=${POLARIS_BOOTSTRAP_CREDENTIALS:-root:secret}
CATALOG_NAME=${POLARIS_CATALOG_NAME:-local_lakehouse}
NAMESPACE=${POLARIS_NAMESPACE:-demo}
USER_NAME=${POLARIS_BOOTSTRAP_CREDENTIALS%%:*}
USER_PASS=${POLARIS_BOOTSTRAP_CREDENTIALS#*:}

TOKEN=$(curl -fsS -X POST "${POLARIS_URI}/api/catalog/v1/oauth/tokens" \
  -H 'Polaris-Realm: default-realm' \
  -u root:secret \
  -d "grant_type=client_credentials" \
  -d "scope=PRINCIPAL_ROLE:ALL" \
  | sed -n 's/.*"access_token"[ ]*:[ ]*"\([^"]*\)".*/\1/p' || true)

if [[ -z "${TOKEN}" ]]; then
  echo "[polaris] Could not fetch auth token automatically."
  echo "[polaris] Manual fallback: create catalog '${CATALOG_NAME}' and namespace '${NAMESPACE}' in Polaris UI/API."
  exit 0
fi

curl -fsS -X POST '${POLARIS_URI}/api/management/v1/catalogs' \
-H 'Polaris-Realm: default-realm' \
-H 'Content-Type: application/json' \
-H 'Authorization: Bearer ${TOKEN}' \
-d '{
  "catalog": {
    "name": "${CATALOG_NAME}",
    "type": "INTERNAL",
    "properties": {
      "default-base-location": "s3://lakehouse/warehouse"
    },
    "storageConfigInfo": {
      "storageType": "S3",
      "allowedLocations": ["s3://lakehouse/"],
      "roleArn": "arn:aws:iam::000000000000:role/polaris-minio",
      "region": "us-east-1"
    }
  }
}'

curl -fsS -X POST '${POLARIS_URI}/api/catalog/v1/${CATALOG_NAME}/namespaces' \
--header 'Polaris-Realm: default-realm' \
--header 'Content-Type: application/json' \
--header 'Authorization: Bearer ${TOKEN}' \
--data '{
    "namespace": [
        "demo"
    ]
}'
echo "[polaris] bootstrap attempted for catalog=${CATALOG_NAME}, namespace=${NAMESPACE}"
