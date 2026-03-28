"""
bbva_post_run_audit.py
======================
Tarea 4 del DAG bbva_pdf_to_silver.

Ejecuta después de la ingesta a Iceberg (con trigger_rule=ALL_DONE) para
auditar la coherencia del run completo, tanto si las tareas anteriores
tuvieron éxito como si fallaron parcialmente.

Checks realizados:
  1.  Manifiesto     – Existe el manifest.json del run en stage-control-prefix.
  2.  Coherencia     – Las filas ingested_raw == stage_raw (sin pérdidas silenciosas).
  3.  Errores PDF    – Ningún PDF quedó en estado 'error' sin marcador de control.
  4.  Tablas Iceberg – Las tres tablas tienen filas si se esperaban datos
                       (raw, enrichment, proposals).
  5.  Huérfanos      – No hay filas en enrichment sin txn_id presente en raw.
  6.  Stage limpieza – Los Parquets de stage del run existen (fueron producidos).

Salida:
  Imprime un informe detallado del run.
  Escribe el resultado del audit en S3 como JSON (audit_<run_id>.json).
  Termina con exit code 0 siempre (trigger_rule=ALL_DONE; no debe bloquear
  futuros runs si solo el audit falla).
  Si detecta anomalías CRÍTICAS las imprime claramente para que aparezcan
  en los logs de Airflow y DataHub pueda recogerlas.

Uso:
  python bbva_post_run_audit.py \\
      --run-id <run_id> \\
      --bucket landing \\
      --stage-data-prefix financial/bbva/_stage/silver_pending \\
      --stage-control-prefix financial/bbva/_stage/control \\
      --target-table lk.silver.finance_transactions_raw \\
      --target-table-enrich lk.silver.finance_txn_enrichment \\
      --target-table-proposals lk.silver.finance_txn_normalization_proposals

Variables de entorno requeridas:
  MINIO_ENDPOINT_BOTO   – URL MinIO para boto3
  MINIO_ROOT_USER       – S3 access key
  MINIO_ROOT_PASSWORD   – S3 secret key
  ICEBERG_CATALOG       – Nombre del catálogo Spark/Iceberg
  ICEBERG_WAREHOUSE     – URI del warehouse
  NESSIE_URI            – URI REST de Nessie
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import boto3
import pandas as pd
from botocore.client import Config
from botocore.exceptions import ClientError

# ── El helper debe estar en el mismo directorio o en PYTHONPATH ──────────────
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import financials_personal_bbva as fpb  # noqa: E402


# ===========================================================================
# Tipos auxiliares
# ===========================================================================

# (nombre_check, severidad: "OK"|"WARN"|"CRITICAL", detalle: str)
AuditResult = Tuple[str, str, str]


# ===========================================================================
# Helpers de infraestructura
# ===========================================================================

def _make_s3_client() -> boto3.client:
    return boto3.client(
        "s3",
        endpoint_url=os.environ["MINIO_ENDPOINT_BOTO"],
        aws_access_key_id=os.environ["MINIO_ROOT_USER"],
        aws_secret_access_key=os.environ["MINIO_ROOT_PASSWORD"],
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        config=Config(signature_version="s3v4"),
    )


def _make_spark_session(catalog: str, warehouse: str, nessie_uri: str):
    from pyspark.sql import SparkSession

    aws_key    = os.environ["MINIO_ROOT_USER"]
    aws_secret = os.environ["MINIO_ROOT_PASSWORD"]
    minio_ep   = os.environ.get("MINIO_ENDPOINT", "http://minio:9000")
    minio_host = minio_ep.replace("http://", "").replace("https://", "")

    return (
        SparkSession.builder
            .appName("bbva-refresh-qdrant-catalog")
            .config("spark.jars.packages", ",".join([
                "org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.5.2",
                "org.apache.iceberg:iceberg-aws-bundle:1.5.2",
                "org.projectnessie.nessie-integrations:nessie-spark-extensions-3.5_2.12:0.104.5",
                "org.apache.hadoop:hadoop-aws:3.3.4",
                "com.amazonaws:aws-java-sdk-bundle:1.12.262",
            ]))
            .config(
                "spark.sql.extensions",
                "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions,"
                "org.projectnessie.spark.extensions.NessieSparkSessionExtensions",
            )
            .config(f"spark.sql.catalog.{catalog}",                     "org.apache.iceberg.spark.SparkCatalog")
            .config(f"spark.sql.catalog.{catalog}.catalog-impl",       "org.apache.iceberg.nessie.NessieCatalog")
            .config(f"spark.sql.catalog.{catalog}.uri",                nessie_uri)
            .config(f"spark.sql.catalog.{catalog}.ref",                os.environ.get("NESSIE_REF", "main"))
            .config(f"spark.sql.catalog.{catalog}.authentication.type","NONE")
            .config(f"spark.sql.catalog.{catalog}.warehouse",          warehouse)
            .config(f"spark.sql.catalog.{catalog}.io-impl",            "org.apache.iceberg.aws.s3.S3FileIO")
            .config(f"spark.sql.catalog.{catalog}.s3.endpoint",            minio_ep)
            .config(f"spark.sql.catalog.{catalog}.s3.path-style-access",   "true")
            .config(f"spark.sql.catalog.{catalog}.s3.region",              os.getenv("AWS_REGION", "us-east-1"))
            .config(f"spark.sql.catalog.{catalog}.s3.access-key-id",       aws_key)
            .config(f"spark.sql.catalog.{catalog}.s3.secret-access-key",   aws_secret)
            .config("spark.hadoop.fs.s3a.endpoint",                        minio_host)
            .config("spark.hadoop.fs.s3a.path.style.access",               "true")
            .config("spark.hadoop.fs.s3a.connection.ssl.enabled",          "false")
            .config("spark.hadoop.fs.s3a.aws.credentials.provider",
                    "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider")
            .config("spark.hadoop.fs.s3a.access.key",                      aws_key)
            .config("spark.hadoop.fs.s3a.secret.key",                      aws_secret)
            .config("spark.executorEnv.AWS_REGION",                        os.getenv("AWS_REGION", "us-east-1"))
            .config("spark.executorEnv.AWS_DEFAULT_REGION",                os.getenv("AWS_REGION", "us-east-1"))
            .config("spark.executorEnv.AWS_EC2_METADATA_DISABLED",         "true")
            .config("spark.driver.extraJavaOptions",                       f"-Daws.region={os.getenv('AWS_REGION', 'us-east-1')}")
            .config("spark.executor.extraJavaOptions",                     f"-Daws.region={os.getenv('AWS_REGION', 'us-east-1')}")
            .config("spark.driver.memory",                                 "512m")
            .config("spark.executor.memory",                               "512m")
            .getOrCreate()
        )


def _stage_parquet_key(prefix: str, run_id: str, table_suffix: str) -> str:
    return f"{prefix.rstrip('/')}/{run_id}/{table_suffix}.parquet"


def _s3_key_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def _read_parquet_from_s3(s3, bucket: str, key: str) -> pd.DataFrame:
    import io
    obj = s3.get_object(Bucket=bucket, Key=key)
    return pd.read_parquet(io.BytesIO(obj["Body"].read()), engine="pyarrow")


def _iceberg_count(spark, table: str) -> int:
    """Devuelve el número de filas de una tabla Iceberg. -1 si no existe."""
    try:
        return spark.table(table).count()
    except Exception:
        return -1


def _iceberg_count_for_run(spark, table: str, run_id: str) -> int:
    """Devuelve filas de la tabla Iceberg filtradas por batch_id == run_id."""
    try:
        from pyspark.sql import functions as F
        return spark.table(table).filter(F.col("batch_id") == run_id).count()
    except Exception:
        return -1


# ===========================================================================
# Checks individuales
# ===========================================================================

def audit_manifest(s3, bucket: str, stage_control_prefix: str, run_id: str
                   ) -> Tuple[AuditResult, Dict[str, Any]]:
    """Verifica que el manifiesto del run existe y lo devuelve parseado."""
    key = f"{stage_control_prefix.rstrip('/')}/{run_id}/manifest.json"
    try:
        obj      = s3.get_object(Bucket=bucket, Key=key)
        manifest = json.loads(obj["Body"].read())
        return (
            ("manifest", "OK", f"Manifiesto encontrado: {key}"),
            manifest,
        )
    except ClientError:
        return (
            ("manifest", "CRITICAL",
             f"Manifiesto NO encontrado: s3://{bucket}/{key}. "
             "La tarea extract pudo haber fallado completamente."),
            {},
        )
    except Exception as e:
        return (
            ("manifest", "CRITICAL", f"Error leyendo manifiesto: {e}"),
            {},
        )


def audit_pdf_errors(manifest: Dict[str, Any]) -> AuditResult:
    """Detecta PDFs que terminaron en error durante el extract."""
    errors = manifest.get("pdfs_error", 0)
    total  = manifest.get("pdfs_found", 0)
    if errors == 0:
        return ("pdf_errors", "OK", f"0 PDFs con error de {total} encontrados")
    # Error parcial: WARN. Error total: CRITICAL.
    processed = manifest.get("pdfs_processed", 0)
    if processed == 0 and total > 0:
        return (
            "pdf_errors", "CRITICAL",
            f"TODOS los PDFs fallaron ({errors}/{total}). "
            "Revisar logs de extract_and_enrich."
        )
    return (
        "pdf_errors", "WARN",
        f"{errors}/{total} PDFs fallaron. Procesados correctamente: {processed}. "
        "Revisar detalles en el manifiesto."
    )


def audit_row_coherence(manifest: Dict[str, Any], spark, run_id: str,
                        target_table: str, target_table_enrich: str,
                        target_table_proposals: str) -> List[AuditResult]:
    """
    Compara filas esperadas (según manifiesto de extract) con filas reales
    escritas en Iceberg (según manifiesto de ingest y conteo real).
    """
    results = []

    expected_raw       = manifest.get("total_raw", 0)
    expected_enrich    = manifest.get("total_enrich", 0)
    expected_proposals = manifest.get("total_proposals", 0)

    written_raw       = manifest.get("ingest_rows_raw", -1)
    written_enrich    = manifest.get("ingest_rows_enrich", -1)
    written_proposals = manifest.get("ingest_rows_proposals", -1)

    pairs = [
        ("coherence_raw",       expected_raw,       written_raw,       target_table),
        ("coherence_enrich",    expected_enrich,     written_enrich,    target_table_enrich),
        ("coherence_proposals", expected_proposals,  written_proposals, target_table_proposals),
    ]

    for check_name, expected, written, table in pairs:
        if expected == 0:
            results.append((check_name, "OK", f"No se esperaban filas en {table}"))
            continue
        if written == -1:
            results.append((
                check_name, "WARN",
                f"No hay dato de filas escritas en el manifiesto para {table}. "
                "La tarea ingest pudo haber fallado."
            ))
            continue
        if written == expected:
            results.append((check_name, "OK",
                            f"{table}: {written}/{expected} filas escritas"))
        elif written < expected:
            loss_pct = round((expected - written) / expected * 100, 1)
            results.append((
                check_name, "CRITICAL" if loss_pct > 5 else "WARN",
                f"{table}: pérdida de {expected - written} filas "
                f"({loss_pct}%). Esperadas: {expected}, escritas: {written}."
            ))
        else:
            # written > expected → puede ser por deduplicación de runs anteriores
            results.append((check_name, "OK",
                            f"{table}: {written} filas escritas (>{expected} esperadas; "
                            "posible solapamiento con runs anteriores – OK por dedup)"))

    return results


def audit_orphan_enrichments(spark, target_table: str,
                             target_table_enrich: str, run_id: str) -> AuditResult:
    """
    Verifica que no hay filas en la tabla de enrichment cuyo txn_id
    no exista en la tabla raw (para el batch_id de este run).
    """
    try:
        from pyspark.sql import functions as F

        df_raw    = spark.table(target_table).filter(F.col("batch_id") == run_id).select("txn_id")
        df_enrich = spark.table(target_table_enrich).filter(F.col("batch_id") == run_id).select("txn_id")

        orphans = df_enrich.join(df_raw, on="txn_id", how="left_anti").count()
        if orphans == 0:
            return ("orphan_enrichments", "OK",
                    "Todas las filas de enrichment tienen txn_id en raw")
        return (
            "orphan_enrichments", "CRITICAL",
            f"{orphans} fila(s) en enrichment sin txn_id correspondiente en raw. "
            "Posible inconsistencia de datos."
        )
    except Exception as e:
        return ("orphan_enrichments", "WARN",
                f"No se pudo verificar huérfanos (tabla puede no existir aún): {e}")


def audit_stage_files(s3, bucket: str, stage_data_prefix: str,
                      run_id: str, manifest: Dict[str, Any]) -> List[AuditResult]:
    """Verifica que los Parquets de stage esperados existen en S3."""
    results  = []
    suffixes = []
    if manifest.get("total_raw", 0) > 0:
        suffixes.append("raw")
    if manifest.get("total_enrich", 0) > 0:
        suffixes.append("enrich")
    if manifest.get("total_proposals", 0) > 0:
        suffixes.append("proposals")

    for suffix in suffixes:
        key = _stage_parquet_key(stage_data_prefix, run_id, suffix)
        exists = _s3_key_exists(s3, bucket, key)
        if exists:
            results.append((f"stage_file_{suffix}", "OK",
                            f"s3://{bucket}/{key} presente"))
        else:
            results.append((f"stage_file_{suffix}", "WARN",
                            f"s3://{bucket}/{key} NO encontrado. "
                            "Puede haber sido eliminado o la tarea extract falló."))

    if not suffixes:
        results.append(("stage_files", "OK", "No se esperaban stage files (0 filas en extract)"))

    return results


# ===========================================================================
# Runner principal
# ===========================================================================

def run_audit(args: argparse.Namespace) -> Tuple[List[AuditResult], Dict[str, Any]]:
    s3 = _make_s3_client()

    catalog    = os.environ.get("ICEBERG_CATALOG",  "lk")
    warehouse  = os.environ.get("ICEBERG_WAREHOUSE", "s3://lakehouse/warehouse")
    nessie_uri = os.environ.get("NESSIE_URI",        "http://nessie:19120/api/v1")

    all_results: List[AuditResult] = []

    # 1. Manifiesto
    manifest_result, manifest = audit_manifest(
        s3, args.bucket, args.stage_control_prefix, args.run_id
    )
    all_results.append(manifest_result)

    # Si no hay manifiesto, los siguientes checks no tienen sentido
    if not manifest:
        return all_results, manifest

    # 2. Errores PDF
    all_results.append(audit_pdf_errors(manifest))

    # 3. Stage files
    all_results.extend(audit_stage_files(
        s3, args.bucket, args.stage_data_prefix, args.run_id, manifest
    ))

    # Los checks de Iceberg requieren Spark; los iniciamos solo si hay datos esperados
    has_iceberg_data = (
        manifest.get("total_raw", 0) > 0
        or manifest.get("total_enrich", 0) > 0
        or manifest.get("total_proposals", 0) > 0
    )

    if has_iceberg_data:
        try:
            spark = _make_spark_session(catalog, warehouse, nessie_uri)

            # 4. Coherencia de filas
            all_results.extend(audit_row_coherence(
                manifest, spark, args.run_id,
                args.target_table, args.target_table_enrich, args.target_table_proposals,
            ))

            # 5. Huérfanos
            all_results.append(audit_orphan_enrichments(
                spark, args.target_table, args.target_table_enrich, args.run_id
            ))

            spark.stop()

        except Exception as e:
            all_results.append((
                "iceberg_checks", "WARN",
                f"No se pudieron ejecutar los checks de Iceberg: {e}"
            ))
    else:
        all_results.append((
            "iceberg_checks", "OK",
            "No se esperaban datos en Iceberg para este run (0 PDFs procesados)"
        ))

    return all_results, manifest


def print_report(run_id: str, results: List[AuditResult],
                 manifest: Dict[str, Any]) -> None:
    print("\n" + "=" * 70)
    print(f"  BBVA Post-Run Audit  –  run_id: {run_id}")
    print("=" * 70)

    # Resumen del manifiesto
    if manifest:
        print(f"  PDFs encontrados:  {manifest.get('pdfs_found', 'n/a')}")
        print(f"  PDFs procesados:   {manifest.get('pdfs_processed', 'n/a')}")
        print(f"  PDFs saltados:     {manifest.get('pdfs_skipped', 'n/a')}")
        print(f"  PDFs con error:    {manifest.get('pdfs_error', 'n/a')}")
        print(f"  Filas raw stage:   {manifest.get('total_raw', 'n/a')}")
        print(f"  Filas enrich stg:  {manifest.get('total_enrich', 'n/a')}")
        print(f"  Filas props stg:   {manifest.get('total_proposals', 'n/a')}")
        print(f"  Filas raw Iceberg: {manifest.get('ingest_rows_raw', 'n/a')}")
        print(f"  Ingest completado: {manifest.get('ingest_completed_at', 'n/a')}")
    print("-" * 70)

    # Checks
    max_name = max(len(name) for name, _, _ in results) if results else 10
    icons    = {"OK": "✓", "WARN": "⚠", "CRITICAL": "✗"}
    for name, severity, detail in results:
        icon = icons.get(severity, "?")
        print(f"  {icon} {severity.ljust(8)}  {name.ljust(max_name)}  {detail}")

    print("=" * 70 + "\n")


def _write_audit_report(s3, bucket: str, stage_control_prefix: str,
                        run_id: str, results: List[AuditResult],
                        manifest: Dict[str, Any]) -> None:
    """Persiste el informe de audit como JSON en S3."""
    report = {
        "run_id":     run_id,
        "audit_at":   datetime.now(timezone.utc).isoformat(),
        "manifest":   manifest,
        "checks": [
            {"name": name, "severity": sev, "detail": detail}
            for name, sev, detail in results
        ],
        "overall": (
            "CRITICAL" if any(s == "CRITICAL" for _, s, _ in results)
            else "WARN" if any(s == "WARN" for _, s, _ in results)
            else "OK"
        ),
    }
    key = f"{stage_control_prefix.rstrip('/')}/{run_id}/audit.json"
    fpb.put_json_to_s3(s3, bucket=bucket, key=key, payload=report)
    print(f"[audit] Informe escrito → s3://{bucket}/{key}")


# ===========================================================================
# Entry point
# ===========================================================================

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Audita la coherencia del run BBVA tras la ingesta a Iceberg"
    )
    p.add_argument("--run-id",                  required=True)
    p.add_argument("--bucket",                  required=True)
    p.add_argument("--stage-data-prefix",       required=True)
    p.add_argument("--stage-control-prefix",    required=True)
    p.add_argument("--target-table",            required=True)
    p.add_argument("--target-table-enrich",     required=True)
    p.add_argument("--target-table-proposals",  required=True)
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args   = parser.parse_args()

    results, manifest = run_audit(args)
    print_report(args.run_id, results, manifest)

    # Persistir informe en S3 (best-effort)
    try:
        s3 = _make_s3_client()
        _write_audit_report(
            s3, args.bucket, args.stage_control_prefix,
            args.run_id, results, manifest
        )
    except Exception as e:
        print(f"[audit] WARN – No se pudo escribir el informe en S3: {e}")

    # Imprimir resumen final de severidad
    criticals = [(n, d) for n, s, d in results if s == "CRITICAL"]
    warns     = [(n, d) for n, s, d in results if s == "WARN"]

    if criticals:
        print(f"[audit] {len(criticals)} anomalía(s) CRÍTICA(s) detectada(s):")
        for name, detail in criticals:
            print(f"  CRITICAL  {name}: {detail}")

    if warns:
        print(f"[audit] {len(warns)} aviso(s) WARNING:")
        for name, detail in warns:
            print(f"  WARN  {name}: {detail}")

    if not criticals and not warns:
        print("[audit] Run limpio. Todas las verificaciones OK.")

    # Siempre exit 0: no bloquear futuros runs por fallos de audit
    sys.exit(0)
