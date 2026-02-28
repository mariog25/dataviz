# data-workloads

Versioned ETL jobs/notebooks for the local Iceberg lakehouse stack.

## Structure
- `spark_jobs/write_demo_table.py`: creates `lakehouse.demo.sample_orders`.
- `spark_jobs/transform_gold_example.py`: placeholder transform job.
- `notebooks/`: exploratory notebooks.
- `dags/`: optional Airflow DAGs.
- `tests/`: basic test skeleton.

## Run the demo Spark job
From `platform-infra/` after stack startup:
```bash
docker compose exec spark spark-submit /opt/data-workloads/spark_jobs/write_demo_table.py
```
Validate from Trino:
```bash
docker compose exec trino trino --server http://localhost:8080 --execute "SELECT * FROM iceberg.demo.sample_orders"
```
=======
## Local tests
```bash
python -m pytest tests -q
```
