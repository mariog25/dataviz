import os
from pathlib import Path

from dagster import Definitions
from dagstermill import (
    ConfigurableLocalOutputNotebookIOManager,
    define_dagstermill_asset,
)

from datahub.ingestion.graph.client import DatahubClientConfig
from datahub_dagster_plugin.sensors.datahub_sensors import (
    DatahubDagsterSourceConfig,
    make_datahub_sensor,
)

BASE_DIR = Path(__file__).resolve().parent
NOTEBOOKS_DIR = BASE_DIR / "notebooks"
OUTPUT_NOTEBOOKS_DIR = BASE_DIR / "notebook_outputs"

DATAHUB_GMS_URL = os.environ.get("DATAHUB_GMS_URL", "http://datahub-gms:8080")
DATAHUB_TOKEN = os.environ.get("DATAHUB_TOKEN")
DAGSTER_UI_URL = os.environ.get("DAGSTER_UI_URL", "http://dagster-webserver:3000")

# 1) Assets notebook
test = define_dagstermill_asset(
    name="test",
    notebook_path=str(NOTEBOOKS_DIR / "hello_etl.ipynb"),
    group_name="debug",
)

bbva_gmail = define_dagstermill_asset(
    name="bbva_gmail",
    notebook_path=str(NOTEBOOKS_DIR / "BBVA/movimientos_bbva/Gmail-BBVA.ipynb"),
    group_name="bbva",
    key_prefix=["prod", "trino", "iceberg", "landing"],
)

bbva_movements = define_dagstermill_asset(
    name="bbva_movements",
    notebook_path=str(NOTEBOOKS_DIR / "BBVA/movimientos_bbva/process_movements.ipynb"),
    group_name="bbva",
    key_prefix=["prod", "trino", "iceberg", "silver"],
    deps=[bbva_gmail],
)

bbva_gold = define_dagstermill_asset(
    name="bbva_gold",
    notebook_path=str(NOTEBOOKS_DIR / "BBVA/gold_bbva/gold_model_spark_bbva.ipynb"),
    group_name="bbva",
    key_prefix=["prod", "trino", "iceberg", "gold"],
    deps=[bbva_movements],
)

# 2) Sensor DataHub
datahub_sensor = make_datahub_sensor(
    config=DatahubDagsterSourceConfig(
        datahub_client_config=DatahubClientConfig(
            server=DATAHUB_GMS_URL,
            token=DATAHUB_TOKEN,
        ),
        dagster_url=DAGSTER_UI_URL,
        capture_asset_materialization=True,
        capture_dataset_from_asset_key=True,
        enable_asset_query_metadata_parsing=True,
        emit_queries=False,
        emit_assets=True,
        connect_ops_to_ops=False,
        materialize_dependencies=False,
        debug_mode=True,
        platform_instance="lakehouse-home",
    )
)

defs = Definitions(
    assets=[test, bbva_gmail, bbva_movements, bbva_gold],
    sensors=[datahub_sensor],
    resources={
        "output_notebook_io_manager": ConfigurableLocalOutputNotebookIOManager(
            base_dir=str(OUTPUT_NOTEBOOKS_DIR)
        ),
    },
)