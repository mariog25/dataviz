# Iceberg naming conventions

- Catalog: `lakehouse`
- Namespaces:
  - `bronze`: raw landed data
  - `silver`: cleaned/conformed data
  - `gold`: curated serving layer
  - `demo`: sandbox/test objects
- Table names: snake_case, plural where meaningful (`sample_orders`, `customer_profiles`)
- Partitioning: start simple (date or bucket) and avoid over-partitioning for local dev
