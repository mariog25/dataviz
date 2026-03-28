from datetime import datetime, timedelta
from pathlib import Path

from airflow import DAG
from airflow.models.baseoperator import chain
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from airflow.operators.python import PythonOperator
from airflow.exceptions import AirflowException
from pathlib import Path
from airflow.providers.common.sql.hooks.sql import DbApiHook
from airflow.datasets import Dataset

# ===========================================================================
# Configuración global
# ===========================================================================

DEFAULT_ARGS = {
    "owner":            "data-platform",
    "depends_on_past":  False,
    "retries":          1,
    "retry_delay":      timedelta(minutes=10),
    "email_on_failure": False,
    "email_on_retry":   False,
}

ENV = "DEV"
TRINO_CONN_ID = "TrinoConnection"

SQL_DIR = Path("/opt/airflow/dags/sql")

SQL_BUILD_FILE = "financial_gold_build.sql"
SQL_VALIDATE_FILE = "financial_gold_validate.sql"

SQL_BUILD_PATH = f"{SQL_DIR}/{SQL_BUILD_FILE}"
SQL_VALIDATE_PATH = f"{SQL_DIR}/{SQL_VALIDATE_FILE}"

# ===========================================================================
# Datasets DataHub
# ===========================================================================

# Inlets silver
SILVER_TXN_RAW_DS = Dataset("iceberg://silver/finance_transactions_raw")
SILVER_TXN_ENRICH_DS = Dataset("iceberg://silver/finance_txn_enrichment")

SILVER_REF_CATEGORIES_DS = Dataset("iceberg://silver/ref_finance_categories")
SILVER_MORTGAGE_PAYMENTS_DS = Dataset("iceberg://silver/finance_mortgage_payments")
SILVER_REF_INTEREST_TYPES_DS = Dataset("iceberg://silver/ref_finance_interest_types")

# Outlets gold - facts
GOLD_FACT_BANK_TXN_DS = Dataset("iceberg://gold/fact_bank_transaction")
GOLD_FACT_MORTGAGE_DS = Dataset("iceberg://gold/fact_mortgage_payment")

# Outlets gold - views
GOLD_VW_BANK_TXN_DS = Dataset("iceberg://gold/vw_bank_transaction_enriched")
GOLD_VW_MORTGAGE_DS = Dataset("iceberg://gold/vw_mortgage_payment_enriched")

# ===========================================================================
# Helpers
# ===========================================================================

def _assert_sql_files_exist():
    required = [
        SQL_BUILD_PATH,
        SQL_VALIDATE_PATH,
    ]
    for path in required:
        if not Path(path).is_file():
            raise AirflowException(f"SQL file not found: {path}")

def load_sql_statements(filename: str) -> list[str]:
    raw_sql = (SQL_DIR / filename).read_text(encoding="utf-8")

    statements = []
    for chunk in raw_sql.split(";"):
        stmt = chunk.strip()
        if stmt:
            statements.append(stmt)

    return statements
# ===========================================================================
# DAG
# ===========================================================================

_DAG_DOC = """
## Financial Gold Datamart

Construye el datamart gold financiero a partir de la capa silver:
- estrella de movimientos bancarios
- estrella de pagos hipotecarios
- vistas semánticas de consumo

### Inputs silver
- lk.silver.finance_transactions_raw
- lk.silver.finance_txn_enrichment
- lk.silver.ref_finance_categories
- lk.silver.finance_mortgage_payments
- lk.silver.ref_finance_interest_types

### Outputs gold
- iceberg.gold.fact_bank_transaction
- iceberg.gold.fact_mortgage_payment
- iceberg.gold.vw_bank_transaction_enriched
- iceberg.gold.vw_mortgage_payment_enriched
"""

with DAG(
    dag_id="financial_gold_datamart",
    description="Build financial gold datamart on Trino/Iceberg.",
    doc_md=_DAG_DOC,
    default_args=DEFAULT_ARGS,
    start_date=datetime(2026, 3, 28),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    tags=["finance", "gold", "datamart", "trino", "iceberg", "datahub"],
    owner_links={"data-platform": "mailto:data-platform@company.com"},
    template_searchpath=["/opt/airflow/dags/sql"]
) as dag:

    check_gold_prerequisites = PythonOperator(
        task_id="check_gold_prerequisites",
        python_callable=_assert_sql_files_exist,
        inlets=[
            SILVER_TXN_RAW_DS,
            SILVER_TXN_ENRICH_DS,
            SILVER_REF_CATEGORIES_DS,
            SILVER_MORTGAGE_PAYMENTS_DS,
            SILVER_REF_INTEREST_TYPES_DS,
        ],
        outlets=[],
    )

    build_gold_datamart = SQLExecuteQueryOperator(
        task_id="build_gold_datamart",
        conn_id=TRINO_CONN_ID,
        sql=load_sql_statements("financial_gold_build.sql"),
        split_statements=True,
        return_last=False,
        inlets=[
            SILVER_TXN_RAW_DS,
            SILVER_TXN_ENRICH_DS,
            SILVER_REF_CATEGORIES_DS,
            SILVER_MORTGAGE_PAYMENTS_DS,
            SILVER_REF_INTEREST_TYPES_DS,
        ],
        outlets=[
            GOLD_FACT_BANK_TXN_DS,
            GOLD_FACT_MORTGAGE_DS,
            GOLD_VW_BANK_TXN_DS,
            GOLD_VW_MORTGAGE_DS,
        ],
    )

    validate_gold_datamart = SQLExecuteQueryOperator(
        task_id="validate_gold_datamart",
        conn_id=TRINO_CONN_ID,
        sql=load_sql_statements("financial_gold_validate.sql"),
        split_statements=True,
        return_last=False,
        inlets=[
            GOLD_FACT_BANK_TXN_DS,
            GOLD_FACT_MORTGAGE_DS,
            GOLD_VW_BANK_TXN_DS,
            GOLD_VW_MORTGAGE_DS,
        ],
        outlets=[],
    )

    chain(
        check_gold_prerequisites,
        build_gold_datamart,
        validate_gold_datamart,
    )