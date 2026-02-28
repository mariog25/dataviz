# platform-infra

Local Docker Compose runtime for a Databricks-like OSS lakehouse MVP:
- **Apache Spark 3.5.1** for ETL
- **Apache Iceberg 1.6.1** tables in **MinIO**
- **Apache Polaris 1.0.1** as REST catalog
- **Trino 457** SQL/JDBC endpoint
- **DataHub 0.13.3** (GMS + frontend + deps)
- **JupyterLab** for notebook exploration

> Hardware target: Windows 11 + Docker Desktop (WSL2), 32 GB RAM.
> Compose memory limits are tuned for a ~20 GB total cap and low idle usage.

## Prerequisites
- Docker Desktop 4.x+ with Compose v2
- At least 20 GB Docker memory available

## Quick start
```bash
cd platform-infra
cp .env.example .env
docker compose up -d --build
```

Run deterministic bootstrap steps:
```bash
./init-scripts/01_minio_create_buckets.sh
./init-scripts/02_bootstrap_polaris.sh
docker compose exec spark spark-submit /opt/data-workloads/spark_jobs/write_demo_table.py
./init-scripts/03_smoke_test.sh
```

## Endpoints
- MinIO API: http://localhost:9000
- MinIO Console: http://localhost:9001
- Polaris API: http://localhost:8181
- Trino: http://localhost:8080
- JupyterLab: http://localhost:8888 (token from `.env`)
- DataHub frontend: http://localhost:9002
- DataHub GMS: http://localhost:8084
- Airflow (optional profile): http://localhost:8081

Enable optional Airflow:
```bash
docker compose --profile airflow up -d airflow
```

## DBeaver (Trino JDBC)
- Driver: Trino
- Host: `localhost`
- Port: `8080`
- Database/Catalog: `iceberg`
- User: `trino`
- Password: _(empty)_
- JDBC URL: `jdbc:trino://localhost:8080/iceberg`

## Sample Trino queries
```sql
SHOW SCHEMAS FROM iceberg;
SHOW TABLES FROM iceberg.demo;
SELECT * FROM iceberg.demo.sample_orders;
```

## Memory tuning notes
- Spark limited to **6 GB** and single executor for quick local ETL iteration.
- Trino capped at **3 GB** with small JVM heap to avoid workstation pressure.
- Polaris capped at **2 GB** with `-Xmx1536m`.
- DataHub stack (all containers together) stays near **5 GB** cap via per-service limits.
- Airflow disabled by default with Compose profile.

## Notes on Polaris bootstrap
`02_bootstrap_polaris.sh` calls Polaris management/catalog APIs. If the API version changes, the script exits with a clear manual fallback instruction to create:
- catalog: `local_lakehouse`
- namespace: `demo`

