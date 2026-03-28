"""
bbva_refresh_qdrant_catalog.py
==============================
Tarea 2 del DAG bbva_pdf_to_silver.

Recarga el catálogo de normalización en Qdrant fusionando dos fuentes:

  1. NORMALIZATION_CATALOG estático (hardcodeado en financials_personal_bbva.py)
     — base de conocimiento inicial, siempre presente.

  2. Registros validados en Iceberg (lk.silver.finance_txn_enrichment)
     — el modelo "aprende" de las validaciones humanas acumuladas en BBDD.
     Solo se indexan filas con validation_status = 'validated'.

Estrategia de embeddings
------------------------
El texto que se embede por cada punto combina TODOS los campos semánticos
relevantes para maximizar el recall en búsquedas aproximadas:

    merchant_norm: <x> | canonical_label: <x> | category_id: <x> |
    category_l1: <x> | category_l2: <x> | concept_norm: <x> | location: <x> |
    aliases: <x>

Los campos concept_norm y location (disponibles solo en los registros validados
de Iceberg) añaden contexto real de transacciones observadas, lo que mejora
el matching semántico frente a transacciones futuras con phrasing similar.

Idempotencia
------------
El script usa upsert (no recreate) por defecto. Cada punto tiene un ID
determinista basado en SHA-256 del texto embebido, de modo que reindexar
el mismo registro produce el mismo ID y simplemente sobreescribe el punto
existente sin duplicar. Usa --recreate solo en migraciones o cambios de
modelo de embeddings.

Uso:
  python bbva_refresh_qdrant_catalog.py \\
      --catalog-source both \\          # static | iceberg | both
      --iceberg-table lk.silver.finance_txn_enrichment \\
      --recreate false

Variables de entorno requeridas:
  QDRANT_URL          – URL de Qdrant          (ej. http://qdrant:6333)
  QDRANT_COLLECTION   – Nombre de la colección
  OLLAMA_URL          – URL de Ollama
  OLLAMA_EMBED_MODEL  – Nombre del modelo de embeddings
  NESSIE_URI          – URI catálogo Iceberg REST para Spark
  ICEBERG_WAREHOUSE   – URI del warehouse
  ICEBERG_CATALOG     – Nombre del catálogo Spark
  MINIO_ROOT_USER     – S3 access key
  MINIO_ROOT_PASSWORD – S3 secret key
  MINIO_ENDPOINT      – URL MinIO
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path
from typing import List, Dict, Any

# ── Helper en el mismo directorio ───────────────────────────────────────────
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import financials_personal_bbva as fpb  # noqa: E402
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm


# ===========================================================================
# Construcción del texto a embeder
# ===========================================================================

def build_enriched_catalog_text(item: dict) -> str:
    """
    Construye el texto a embeder para un punto del catálogo.

    Combina todos los campos semánticos disponibles:
    - merchant_norm, canonical_label, category_id, category_l1, category_l2
      → presentes tanto en el catálogo estático como en Iceberg
    - concept_norm, location
      → disponibles solo en registros validados de Iceberg; enriquecen
        el embedding con el texto real de las transacciones observadas
    - aliases
      → disponibles en el catálogo estático; proporcionan variantes de escritura

    El resultado es un string pipe-separated que Ollama embedirá como un todo.
    """
    parts = [
        f"merchant_norm: {item.get('merchant_norm', '')}",
        f"canonical_label: {item.get('canonical_label', '')}",
        f"category_id: {item.get('category_id', '')}",
        f"category_l1: {item.get('category_l1', '')}",
        f"category_l2: {item.get('category_l2', '')}",
    ]
    # Campos opcionales — solo si tienen valor
    if item.get("concept_norm"):
        parts.append(f"concept_norm: {item['concept_norm']}")
    if item.get("location"):
        parts.append(f"location: {item['location']}")
    if item.get("aliases"):
        aliases_str = " | ".join(item["aliases"]) if isinstance(item["aliases"], list) else str(item["aliases"])
        parts.append(f"aliases: {aliases_str}")

    return " | ".join(parts)


def point_id_from_text(text: str) -> int:
    """
    Genera un ID entero determinista para un punto Qdrant a partir del texto.
    Usa los primeros 8 bytes del SHA-256 → int de 64 bits.
    Garantiza idempotencia: el mismo texto siempre produce el mismo ID.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


# ===========================================================================
# Fuentes de datos
# ===========================================================================

def load_static_catalog() -> List[Dict[str, Any]]:
    """Devuelve el catálogo estático de financials_personal_bbva.py."""
    return [
        {
            "id":              item["id"],
            "merchant_norm":   item.get("merchant_norm", ""),
            "canonical_label": item.get("canonical_label", ""),
            "category_id":     item.get("category_id", ""),
            "category_l1":     item.get("category_l1", ""),
            "category_l2":     item.get("category_l2", ""),
            "aliases":         item.get("aliases", []),
            "concept_norm":    None,
            "location":        None,
            "source":          item.get("source", "static"),
        }
        for item in fpb.NORMALIZATION_CATALOG
    ]


def load_iceberg_validated(spark, table: str) -> List[Dict[str, Any]]:
    """
    Carga registros validados desde Iceberg y los convierte al formato
    del catálogo de normalización.

    Query:
        SELECT DISTINCT merchant_norm, canonical_label, category_id,
                        category_l1, category_l2, concept_norm, location
        FROM <table>
        WHERE validation_status = 'validated'
          AND merchant_norm IS NOT NULL
          AND category_id IS NOT NULL

    Agrupa por (merchant_norm, canonical_label, category_id) y acumula
    concept_norm únicos como aliases enriquecidos.
    """
    query = f"""
        SELECT DISTINCT
            merchant_norm,
            canonical_label,
            category_id,
            category_l1,
            category_l2
        FROM {table}
        WHERE validation_status = 'validated'
          AND merchant_norm IS NOT NULL
          AND category_id   IS NOT NULL
    """
    try:
        df = spark.sql(query).toPandas()
    except Exception as e:
        print(f"  [refresh] WARN – No se pudo leer {table}: {e}")
        print("  [refresh] Continuando solo con el catálogo estático.")
        return []

    if df.empty:
        print(f"  [refresh] No hay registros validados en {table} aún.")
        return []

    # Agrupar por merchant: acumular concept_norm únicos como aliases enriquecidos
    from collections import defaultdict
    groups: Dict[tuple, Dict] = {}
    concept_norms: Dict[tuple, list] = defaultdict(list)

    for _, row in df.iterrows():
        key = (
            str(row.get("merchant_norm")   or ""),
            str(row.get("canonical_label") or ""),
            str(row.get("category_id")     or ""),
            str(row.get("category_l1")     or ""),
            str(row.get("category_l2")     or ""),
        )
        if key not in groups:
            groups[key] = {
                "merchant_norm":   key[0],
                "canonical_label": key[1],
                "category_id":     key[2],
                "category_l1":     key[3],
                "category_l2":     key[4],
                "location":        str(row.get("location") or "") or None,
                "source":          "iceberg_validated",
            }
        cn = str(row.get("concept_norm") or "").strip()
        if cn and cn not in concept_norms[key]:
            concept_norms[key].append(cn)

    result = []
    for key, item in groups.items():
        import re
        entry_id = re.sub(r'[^a-z0-9]+', '_', item["merchant_norm"].lower()).strip('_')
        item["id"]          = f"iceberg_{entry_id}"
        item["aliases"]     = sorted(set(concept_norms[key]))
        item["concept_norm"] = item["aliases"][0] if item["aliases"] else None
        result.append(item)

    print(f"  [refresh] {len(result)} merchants únicos cargados desde Iceberg ({len(df)} filas validadas)")
    return result


# ===========================================================================
# Indexación en Qdrant
# ===========================================================================

def index_catalog_points(
    qdrant: QdrantClient,
    items: List[Dict[str, Any]],
    source_label: str,
) -> int:
    """
    Embede cada item y hace upsert en Qdrant.

    Usa IDs deterministas (SHA-256 del texto) para garantizar idempotencia.
    Retorna el número de puntos indexados.
    """
    points = []
    skipped = 0

    for item in items:
        text = build_enriched_catalog_text(item)
        if not text.strip():
            skipped += 1
            continue

        try:
            vec = fpb.ollama_embed(text)
        except Exception as e:
            print(f"  [refresh] WARN – Error al embeder '{item.get('merchant_norm')}': {e}")
            skipped += 1
            continue

        point_id = point_id_from_text(text)
        payload  = {
            "id":              item.get("id", ""),
            "merchant_norm":   item.get("merchant_norm", ""),
            "canonical_label": item.get("canonical_label", ""),
            "category_id":     item.get("category_id", ""),
            "category_l1":     item.get("category_l1", ""),
            "category_l2":     item.get("category_l2", ""),
            "aliases":         item.get("aliases", []),
            "concept_norm":    item.get("concept_norm"),
            "location":        item.get("location"),
            "source":          item.get("source", source_label),
            "text":            text,
        }
        points.append(qm.PointStruct(id=point_id, vector=vec, payload=payload))

    if points:
        # Upsert en lotes de 100 para evitar timeouts con colecciones grandes
        batch_size = 100
        for i in range(0, len(points), batch_size):
            batch = points[i: i + batch_size]
            qdrant.upsert(collection_name=fpb.QDRANT_COLLECTION, points=batch)

    indexed = len(points)
    print(f"  [refresh] {source_label}: {indexed} puntos indexados, {skipped} omitidos")
    return indexed


# ===========================================================================
# Runner principal
# ===========================================================================

def run_refresh(args: argparse.Namespace) -> None:
    print(f"[refresh] Iniciando recarga del catálogo Qdrant")
    print(f"[refresh] Fuente: {args.catalog_source}  |  recreate: {args.recreate}")

    qdrant_url  = os.environ.get("QDRANT_URL",       fpb.QDRANT_URL)
    collection  = os.environ.get("QDRANT_COLLECTION", fpb.QDRANT_COLLECTION)
    qdrant      = QdrantClient(url=qdrant_url, timeout=30)

    # ── Recrear colección si se solicita ──
    if args.recreate:
        print(f"  [refresh] Eliminando colección '{collection}'...")
        try:
            qdrant.delete_collection(collection)
        except Exception:
            pass

    fpb.ensure_qdrant_collection(qdrant)
    print(f"  [refresh] Colección '{collection}' lista")

    total_indexed = 0

    # ── Fuente 1: Catálogo estático ──────────────────────────────────────
    if args.catalog_source in ("static", "both"):
        static_items = load_static_catalog()
        print(f"  [refresh] Catálogo estático: {len(static_items)} entradas")
        total_indexed += index_catalog_points(qdrant, static_items, "static")

    # ── Fuente 2: Registros validados en Iceberg ─────────────────────────
    if args.catalog_source in ("iceberg", "both"):
        catalog    = os.environ.get("ICEBERG_CATALOG",  "lk")
        warehouse  = os.environ.get("ICEBERG_WAREHOUSE", "s3://lakehouse/warehouse")
        nessie_uri = os.environ.get("NESSIE_URI",        "http://nessie:19120/api/v1")

        # Spark session ligera solo para la query de Iceberg
        try:
            from pyspark.sql import SparkSession

            aws_key    = os.environ["MINIO_ROOT_USER"]
            aws_secret = os.environ["MINIO_ROOT_PASSWORD"]
            minio_ep   = os.environ.get("MINIO_ENDPOINT", "http://minio:9000")
            minio_host = minio_ep.replace("http://", "").replace("https://", "")

            spark = (
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
                .config(f"spark.sql.catalog.{catalog}", "org.apache.iceberg.spark.SparkCatalog")
                .config(f"spark.sql.catalog.{catalog}.catalog-impl", "org.apache.iceberg.nessie.NessieCatalog")
                .config(f"spark.sql.catalog.{catalog}.uri", nessie_uri)
                .config(f"spark.sql.catalog.{catalog}.ref", os.environ.get("NESSIE_REF", "main"))
                .config(f"spark.sql.catalog.{catalog}.authentication.type", "NONE")
                .config(f"spark.sql.catalog.{catalog}.warehouse", warehouse)
                .config(f"spark.sql.catalog.{catalog}.io-impl", "org.apache.iceberg.aws.s3.S3FileIO")
                .config(f"spark.sql.catalog.{catalog}.s3.endpoint", minio_ep)
                .config(f"spark.sql.catalog.{catalog}.s3.path-style-access", "true")
                .config(f"spark.sql.catalog.{catalog}.s3.region", os.getenv("AWS_REGION", "us-east-1"))
                .config(f"spark.sql.catalog.{catalog}.s3.access-key-id", aws_key)
                .config(f"spark.sql.catalog.{catalog}.s3.secret-access-key", aws_secret)
                .config("spark.hadoop.fs.s3a.endpoint", minio_host)
                .config("spark.hadoop.fs.s3a.path.style.access", "true")
                .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "false")
                .config(
                    "spark.hadoop.fs.s3a.aws.credentials.provider",
                    "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider",
                )
                .config("spark.hadoop.fs.s3a.access.key", aws_key)
                .config("spark.hadoop.fs.s3a.secret.key", aws_secret)
                .config("spark.executorEnv.AWS_REGION", os.getenv("AWS_REGION", "us-east-1"))
                .config("spark.executorEnv.AWS_DEFAULT_REGION", os.getenv("AWS_REGION", "us-east-1"))
                .config("spark.executorEnv.AWS_EC2_METADATA_DISABLED", "true")
                .config("spark.driver.extraJavaOptions", f"-Daws.region={os.getenv('AWS_REGION', 'us-east-1')}")
                .config("spark.executor.extraJavaOptions", f"-Daws.region={os.getenv('AWS_REGION', 'us-east-1')}")
                .config("spark.driver.memory", "512m")
                .config("spark.executor.memory", "512m")
                .getOrCreate()
            )

            iceberg_items = load_iceberg_validated(spark, args.iceberg_table)
            total_indexed += index_catalog_points(qdrant, iceberg_items, "iceberg_validated")
            spark.stop()

        except Exception as e:
            print(f"  [refresh] ERROR cargando desde Iceberg: {e}")
            print("  [refresh] Continuando con el catálogo estático únicamente.")

    # ── Resumen final ──────────────────────────────────────────────────────
    try:
        collection_info = qdrant.get_collection(collection)
        total_in_qdrant = collection_info.points_count
    except Exception:
        total_in_qdrant = "desconocido"

    print(f"\n[refresh] DONE")
    print(f"  Puntos indexados en este run : {total_indexed}")
    print(f"  Total puntos en colección    : {total_in_qdrant}")


# ===========================================================================
# Entry point
# ===========================================================================

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Recarga el catálogo de normalización en Qdrant desde BBDD validada"
    )
    p.add_argument(
        "--catalog-source",
        choices=["static", "iceberg", "both"],
        default="both",
        help="Fuente de datos: solo estático, solo Iceberg, o ambos (default: both)",
    )
    p.add_argument(
        "--iceberg-table",
        default="lk.silver.finance_txn_enrichment",
        help="Tabla Iceberg con registros validados",
    )
    p.add_argument(
        "--recreate",
        action="store_true",
        default=False,
        help="Eliminar y recrear la colección Qdrant antes de indexar",
    )
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args   = parser.parse_args()
    run_refresh(args)
    sys.exit(0)
