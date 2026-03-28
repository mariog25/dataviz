# airflow/dags/bbva_ingest_reports.py
from __future__ import annotations

import os
import sys
import json
from datetime import datetime, timedelta

from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException
from airflow.utils.timezone import datetime as tz_datetime

from bbva_download_landing import ingest_bbva_reports 


DEFAULT_ARGS = {
    "owner": "airflow",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=10),
}


@dag(
    dag_id="bbva_ingest_reports_monthly",
    description="Download BBVA monthly PDFs from Gmail and store in landing/financial (dedup+manifest+hash).",
    default_args=DEFAULT_ARGS,
    start_date=tz_datetime(2026, 1, 1),  # ajusta si quieres
    schedule="0 6 15 * *",  # día 15 a las 06:00 (Europe/Madrid por defecto en tu Airflow si lo tienes así)
    catchup=False,
    max_active_runs=1,
    tags=["finance", "bbva", "landing"],
)
def bbva_ingest_reports_monthly():

    @task
    def run_ingestion() -> dict:
        """
        Ejecuta ingest_bbva_reports().

        Debe devolver un dict JSON-serializable con un resumen:
          - period
          - processed_count
          - uploaded_count
          - skipped_count
          - landing_prefix
          - manifest_paths (opcional)
          - warnings/errors (opcional)
        """
        # Variables de entorno esperables (ejemplos típicos)
        # - VAULT_ADDR / VAULT_TOKEN (si ingest_bbva_reports lee secretos de Vault)
        # - MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY
        # - LANDING_BUCKET=landing
        # - LANDING_PREFIX=financial/bbva
        # - BBVA_PERIOD=YYYY-MM (si lo soportas)
        #
        # Si ingest_bbva_reports ya lo calcula todo solo, no necesitas pasar nada.

        try:
            result = ingest_bbva_reports()
        except Exception as e:
            raise AirflowFailException(f"ingest_bbva_reports failed: {e}")

        # Normaliza a dict para XCom/logs
        if result is None:
            result = {"status": "OK", "note": "ingest_bbva_reports returned None"}

        if not isinstance(result, dict):
            # si devuelve un objeto, intenta serializarlo
            try:
                result = json.loads(json.dumps(result, default=str))
            except Exception:
                result = {"status": "OK", "result_str": str(result)}

        return result

    @task
    def log_summary(result: dict) -> None:
        """
        Imprime un reporte informativo en logs de Airflow.
        """
        status = result.get("status", "OK")
        period = result.get("period", "-")
        uploaded = result.get("uploaded_count", result.get("uploaded", "-"))
        skipped = result.get("skipped_count", result.get("skipped", "-"))
        processed = result.get("processed_count", result.get("processed", "-"))
        landing = result.get("landing_prefix", result.get("landing_path", "landing/financial"))

        lines = []
        lines.append("=== BBVA Ingestion Summary ===")
        lines.append(f"Status:    {status}")
        lines.append(f"Period:    {period}")
        lines.append(f"Landing:   {landing}")
        lines.append(f"Processed: {processed}")
        lines.append(f"Uploaded:  {uploaded}")
        lines.append(f"Skipped:   {skipped}")

        warnings = result.get("warnings") or []
        errors = result.get("errors") or []

        if warnings:
            lines.append("Warnings:")
            for w in warnings:
                lines.append(f"  - {w}")

        if errors:
            lines.append("Errors:")
            for e in errors:
                lines.append(f"  - {e}")

        print("\n".join(lines))

        if str(status).upper() not in ("OK", "SUCCESS"):
            raise AirflowFailException(f"Ingestion finished with status={status}")

    summary = log_summary(run_ingestion())
    # (si quieres, añade aquí más tasks: registrar en tabla control, notificar, etc.)


bbva_ingest_reports_monthly()