"""
bbva_validate_environment.py
============================
Tarea 1 del DAG bbva_pdf_to_silver.

Comprueba que todos los servicios externos requeridos por el pipeline están
operativos ANTES de descargar ningún PDF ni gastar recursos de cómputo.

Checks realizados (en orden):
  1.  Poppler      – pdftotext y pdfinfo presentes en PATH
  2.  MinIO / S3   – bucket 'landing' accesible con las credenciales configuradas
  3.  Vault        – servidor responde y el token tiene acceso al secreto PDF
  4.  Qdrant       – colección de normalización existe y tiene vectores indexados
  5.  Ollama       – servidor responde y los modelos chat + embed están disponibles
  6.  Nessie       – API REST responde (rama main accesible)
  7.  PDFs         – existen PDFs en el prefijo landing (pipeline no vacío)

Salida:
  Imprime un resumen con el estado de cada check.
  Termina con exit code 0 si todo está OK.
  Termina con exit code 1 en cuanto detecta un fallo bloqueante, listando
  todos los fallos encontrados antes de abortar.

Uso:
  python bbva_validate_environment.py \\
      --bucket landing \\
      --landing-prefix financial/bbva/ \\
      --control-prefix financial/bbva/control \\
      --vault-secret-path cubbyhole/bbva-pdf-key \\
      --vault-secret-key bbva-key

Variables de entorno requeridas:
  MINIO_ENDPOINT_BOTO   – URL MinIO para boto3  (ej. http://minio:9000)
  MINIO_ROOT_USER       – S3 access key
  MINIO_ROOT_PASSWORD   – S3 secret key
  VAULT_ADDR            – URL de Vault           (ej. http://vault:8200)
  VAULT_TOKEN           – Token de autenticación Vault
  VAULT_SECRET_PATH     – Ruta del secreto       (ej. cubbyhole/bbva-pdf-key)
  VAULT_SECRET_KEY      – Clave dentro del secreto (ej. bbva-key)
  OLLAMA_URL            – URL de Ollama          (ej. http://ollama:11434)
  OLLAMA_CHAT_MODEL     – Nombre del modelo chat
  OLLAMA_EMBED_MODEL    – Nombre del modelo embed
  QDRANT_URL            – URL de Qdrant          (ej. http://qdrant:6333)
  QDRANT_COLLECTION     – Nombre de la colección Qdrant
  NESSIE_URI            – URI REST de Nessie     (ej. http://nessie:19120/api/v1)
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

import boto3
import requests
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

# ── El helper debe estar en el mismo directorio o en PYTHONPATH ──────────────
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import financials_personal_bbva as fpb  # noqa: E402


# ===========================================================================
# Tipos auxiliares
# ===========================================================================

# (nombre_check, ok: bool, detalle: str)
CheckResult = Tuple[str, bool, str]


# ===========================================================================
# Checks individuales
# ===========================================================================

def check_poppler() -> CheckResult:
    """Verifica que pdftotext y pdfinfo están en PATH."""
    missing = []
    for tool in ("pdftotext", "pdfinfo"):
        if shutil.which(tool) is None:
            missing.append(tool)
    if missing:
        return ("poppler", False, f"Herramientas no encontradas en PATH: {missing}")
    # Obtener versión para el log
    try:
        result = subprocess.run(
            ["pdftotext", "-v"], capture_output=True, text=True, timeout=5
        )
        version = (result.stderr or result.stdout).strip().splitlines()[0]
    except Exception:
        version = "versión desconocida"
    return ("poppler", True, version)


def check_minio(bucket: str) -> CheckResult:
    """Verifica conectividad con MinIO y acceso al bucket landing."""
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["MINIO_ENDPOINT_BOTO"],
            aws_access_key_id=os.environ["MINIO_ROOT_USER"],
            aws_secret_access_key=os.environ["MINIO_ROOT_PASSWORD"],
            region_name=os.getenv("AWS_REGION", "us-east-1"),
            config=Config(signature_version="s3v4"),
        )
        s3.head_bucket(Bucket=bucket)
        return ("minio", True, f"Bucket '{bucket}' accesible")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        return ("minio", False, f"ClientError {code}: {e}")
    except (BotoCoreError, Exception) as e:
        return ("minio", False, str(e))


def check_vault(secret_path: str, secret_key: str) -> CheckResult:
    """Verifica que Vault responde y el secreto PDF es accesible."""
    vault_addr  = os.environ["VAULT_ADDR"]
    vault_token = os.environ["VAULT_TOKEN"]
    try:
        # Health check
        r = requests.get(f"{vault_addr}/v1/sys/health", timeout=5)
        if r.status_code not in (200, 429, 472, 473):
            return ("vault", False, f"Health endpoint devolvió HTTP {r.status_code}")
        # Leer el secreto
        secret = fpb.read_vault_secret(vault_addr, vault_token, secret_path, secret_key)
        if not secret:
            return ("vault", False, f"Secreto '{secret_path}/{secret_key}' vacío o no encontrado")
        return ("vault", True, f"Secreto '{secret_path}/{secret_key}' leído correctamente")
    except Exception as e:
        return ("vault", False, str(e))


def check_qdrant(collection: str) -> CheckResult:
    """Verifica que Qdrant responde y la colección de normalización tiene puntos."""
    qdrant_url = os.environ.get("QDRANT_URL", fpb.QDRANT_URL)
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(url=qdrant_url, timeout=10)
        info   = client.get_collection(collection)
        count  = info.points_count
        if count == 0:
            return (
                "qdrant", False,
                f"Colección '{collection}' existe pero está vacía (0 vectores). "
                "Ejecuta load_normalization_catalog(recreate=True) primero."
            )
        return ("qdrant", True, f"Colección '{collection}' OK – {count} vectores indexados")
    except Exception as e:
        return ("qdrant", False, str(e))


def check_ollama(chat_model: str, embed_model: str) -> CheckResult:
    """Verifica que Ollama responde y los dos modelos requeridos están disponibles."""
    ollama_url = os.environ.get("OLLAMA_URL", fpb.OLLAMA_URL)
    try:
        r = requests.get(f"{ollama_url}/api/tags", timeout=10)
        r.raise_for_status()
        available = {m["name"] for m in r.json().get("models", [])}
        # Normalizar: set de nombres base (sin tag) para que
        # 'embeddinggemma' matchee con 'embeddinggemma:latest'
        available_base = {name.split(":")[0] for name in available}
        missing = []
        for model in (chat_model, embed_model):
            model_base = model.split(":")[0]
            if model not in available and model_base not in available_base:
                missing.append(model)
        if missing:
            return (
                "ollama", False,
                f"Modelos no disponibles: {missing}. Disponibles: {sorted(available)}"
            )
        return ("ollama", True, f"Modelos '{chat_model}' y '{embed_model}' disponibles")
    except Exception as e:
        return ("ollama", False, str(e))


def check_nessie() -> CheckResult:
    """Verifica que el servidor Nessie responde y la rama main es accesible.

    El health check usa la API REST de Nessie (/api/v1), que es distinta del
    endpoint del catálogo Iceberg REST (/iceberg) usado por Spark.
    NESSIE_URI = http://nessie:19120/iceberg  (catálogo Spark)
    NESSIE_API_URI = http://nessie:19120/api/v1  (API REST Nessie, para checks)
    Si NESSIE_API_URI no está definida se deriva sustituyendo /iceberg por /api/v1.
    """
    nessie_uri = os.environ.get("NESSIE_URI", "http://nessie:19120/api/v1")

    # Resolver endpoint de la API REST (distinto del catálogo Iceberg)
    api_uri = os.environ.get("NESSIE_API_URI", "").strip()
    if not api_uri:
        api_uri = nessie_uri.rstrip("/")
        if api_uri.endswith("/iceberg"):
            api_uri = api_uri[: -len("/iceberg")] + "/api/v1"
        elif not api_uri.endswith("/api/v1"):
            api_uri = api_uri + "/api/v1"

    try:
        r = requests.get(f"{api_uri}/trees/tree/main", timeout=10)
        if r.status_code == 200:
            return ("nessie", True, f"Rama 'main' accesible en {api_uri}")
        return ("nessie", False, f"HTTP {r.status_code} al consultar rama main: {r.text[:200]}")
    except Exception as e:
        return ("nessie", False, str(e))


def check_pdfs_in_landing(bucket: str, landing_prefix: str) -> CheckResult:
    """Verifica que hay al menos un PDF en el prefijo landing de S3."""
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["MINIO_ENDPOINT_BOTO"],
            aws_access_key_id=os.environ["MINIO_ROOT_USER"],
            aws_secret_access_key=os.environ["MINIO_ROOT_PASSWORD"],
            region_name=os.getenv("AWS_REGION", "us-east-1"),
            config=Config(signature_version="s3v4"),
        )
        keys = fpb.list_landing_pdfs(s3, bucket=bucket, prefix=landing_prefix)
        if not keys:
            # No es un error bloqueante: el pipeline simplemente no hace nada.
            # Lo marcamos como WARNING (ok=True con aviso).
            return (
                "pdfs_landing", True,
                f"WARN – No hay PDFs en s3://{bucket}/{landing_prefix}. "
                "El pipeline completará sin procesar nada."
            )
        return ("pdfs_landing", True, f"{len(keys)} PDF(s) encontrados en landing")
    except Exception as e:
        return ("pdfs_landing", False, str(e))


# ===========================================================================
# Runner principal
# ===========================================================================

def run_all_checks(args: argparse.Namespace) -> List[CheckResult]:
    chat_model  = os.environ.get("OLLAMA_CHAT_MODEL",  fpb.OLLAMA_CHAT_MODEL)
    embed_model = os.environ.get("OLLAMA_EMBED_MODEL", fpb.OLLAMA_EMBED_MODEL)
    collection  = os.environ.get("QDRANT_COLLECTION",  fpb.QDRANT_COLLECTION)

    checks = [
        check_poppler(),
        check_minio(args.bucket),
        check_vault(args.vault_secret_path, args.vault_secret_key),
        check_qdrant(collection),
        check_ollama(chat_model, embed_model),
        check_nessie(),
        check_pdfs_in_landing(args.bucket, args.landing_prefix),
    ]
    return checks


def print_summary(checks: List[CheckResult]) -> None:
    print("\n" + "=" * 60)
    print("  BBVA Pipeline – Validación de entorno")
    print("=" * 60)
    max_name = max(len(name) for name, _, _ in checks)
    for name, ok, detail in checks:
        status = "✓ OK  " if ok else "✗ FAIL"
        print(f"  {status}  {name.ljust(max_name)}  {detail}")
    print("=" * 60 + "\n")


# ===========================================================================
# Entry point
# ===========================================================================

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Valida que todos los servicios del pipeline BBVA están operativos"
    )
    p.add_argument("--bucket",            required=True,
                   help="Bucket S3/MinIO landing (ej. landing)")
    p.add_argument("--landing-prefix",    required=True,
                   help="Prefijo S3 donde se buscan los PDFs (ej. financial/bbva/)")
    p.add_argument("--control-prefix",    required=True,
                   help="Prefijo S3 de los marcadores de control (ej. financial/bbva/control)")
    p.add_argument("--vault-secret-path", required=True,
                   help="Ruta del secreto en Vault (ej. cubbyhole/bbva-pdf-key)")
    p.add_argument("--vault-secret-key",  required=True,
                   help="Clave dentro del secreto (ej. bbva-key)")
    return p


if __name__ == "__main__":
    parser = _build_parser()
    args   = parser.parse_args()

    results  = run_all_checks(args)
    print_summary(results)

    failures = [(name, detail) for name, ok, detail in results if not ok]
    if failures:
        print(f"[validate] {len(failures)} check(s) fallaron:")
        for name, detail in failures:
            print(f"  - {name}: {detail}")
        sys.exit(1)

    print("[validate] Todos los checks pasaron. El pipeline puede ejecutarse.")
    sys.exit(0)