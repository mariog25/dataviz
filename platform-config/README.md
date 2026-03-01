# platform-config

Runtime-agnostic declarative configuration for catalog/governance.

## Contents
- `iceberg/`: namespace conventions + bootstrap payload examples.
- `trino/catalogs/`: catalog templates aligned with infra runtime.
- `datahub/`: starter governance artifacts (domains, glossary, tags, ingestion skeleton).
- `policies/`: future policy-as-code location.

## Bootstrap concept
For Polaris-based deployments, create catalog + namespaces from the payload examples in `iceberg/namespaces.sql` (or via REST equivalents).
