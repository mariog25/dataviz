"""
financials_personal_bbva.py
===========================
Librería de utilidades para procesar extractos PDF de BBVA.

Funcionalidades:
  - Extracción de texto de PDFs protegidos con contraseña (Poppler)
  - Parseo de transacciones de cuenta y tarjeta con regex en formato español
  - Clasificación de transacciones en categoría/subcategoría mediante RAG
    (Qdrant para búsqueda semántica + Ollama LLM para casos ambiguos)
  - Escritura de resultados en tablas Apache Iceberg via PySpark
  - Control de idempotencia con marcadores SHA-256 en S3/MinIO

Servicios externos (via variables de entorno):
    OLLAMA_URL          - Servidor Ollama          (default: http://ollama:11434)
    OLLAMA_CHAT_MODEL   - Modelo de chat           (default: qwen2.5:3b-instruct)
    OLLAMA_EMBED_MODEL  - Modelo de embeddings     (default: embeddinggemma)
    QDRANT_URL          - Qdrant vector DB         (default: http://qdrant:6333)
    QDRANT_COLLECTION   - Colección Qdrant         (default: finance_txn_norm_catalog)
    MINIO_ROOT_USER     - S3 / MinIO access key    (default: minioadmin)
    MINIO_ROOT_PASSWORD - S3 / MinIO secret key    (default: minioadmin123)
    VAULT_ADDR          - HashiCorp Vault          (default: http://vault:8200)
    VAULT_TOKEN         - Token de autenticación Vault (requerido)
"""

# ===========================================================================
# ─── IMPORTS ────────────────────────────────────────────────────────────────
# ===========================================================================

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import boto3
import pandas as pd
import requests
from botocore.client import Config
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import (
    BooleanType, DateType, DoubleType,
    IntegerType, StringType, StructField,
    StructType, TimestampType,
)
import pyspark.sql.types as T

# ===========================================================================
# ─── CONFIGURACIÓN GLOBAL ───────────────────────────────────────────────────
# ===========================================================================

# Tablas Iceberg
TARGET_TABLE        = "lk.silver.finance_transactions_raw"
TARGET_TABLE_ENRICH = "lk.silver.finance_txn_enrichment"

# S3 / MinIO
BUCKET                   = "landing"
CONTROL_PREFIX           = "financial/bbva/control"
CONTROL_PROCESSED_PREFIX = f"{CONTROL_PREFIX}/processed"
CONTROL_FAILED_PREFIX    = f"{CONTROL_PREFIX}/failed"
LANDING_PREFIX           = "financial/bbva/"

# Ollama
OLLAMA_URL         = os.getenv("OLLAMA_URL",        "http://ollama:11434")
OLLAMA_CHAT_MODEL  = os.getenv("OLLAMA_CHAT_MODEL",  "qwen2.5:3b-instruct")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "embeddinggemma")

# Qdrant
QDRANT_URL        = os.getenv("QDRANT_URL",        "http://qdrant:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "finance_txn_norm_catalog")

# RAG
RAG_TOP_K                = 3
RAG_DIRECT_HIT_THRESHOLD = 0.65   # Score >= este valor → no se llama al LLM
MAX_BATCH_USER_PROMPT_LEN = 4500  # Aumentado: bloque de categorías con descripciones ~2551 chars
MAX_BATCH_ITEMS           = 4

# Taxonomía de categorías (lista estática en memoria — espejo de lk.silver.ref_finance_categories).
# El campo "description" se incluye en el prompt del LLM para que el modelo entienda
# el significado semántico de cada categoría y no solo su identificador técnico.
# Si actualizas descripciones en ref_finance_categories, actualiza también este dict.
ALLOWED_CATEGORIES: List[Dict] = [
    # ── Alimentación ──────────────────────────────────────────────────────
    {"category_id": "groceries.supermarket",
     "category_l1": "groceries",     "category_l2": "supermarket",
     "description": "Supermercados y tiendas de alimentación (Carrefour, Mercadona, Dia, etc.)"},
    {"category_id": "groceries.amazon",
     "category_l1": "groceries",     "category_l2": "amazon",
     "description": "Compras de alimentación en Amazon Fresh o entregas UFG*"},

    # ── Suministros ───────────────────────────────────────────────────────
    {"category_id": "utilities.telecommunications",
     "category_l1": "utilities",     "category_l2": "telecommunications",
     "description": "Telecomunicaciones e internet: Telefónica, Movistar, Orange, Vodafone"},
    {"category_id": "utilities.energy",
     "category_l1": "utilities",     "category_l2": "energy",
     "description": "Suministro de gas y electricidad: Gas Natural, Iberdrola, Endesa"},
    {"category_id": "utilities.water",
     "category_l1": "utilities",     "category_l2": "water",
     "description": "Suministro de agua: adeudos a cargo de Aqualia, Canal de Isabel II, Agbar, cargos de ullastres por factura agua"},

    # ── Vivienda ──────────────────────────────────────────────────────────
    {"category_id": "housing.community_fees",
     "category_l1": "housing",       "category_l2": "community_fees",
     "description": "Gastos de comunidad de propietarios y derramas"},
     {"category_id": "housing.college",
     "category_l1": "housing",       "category_l2": "college",
     "description": "Cargos de universidades y otros cursos"},
    # ── Transporte ────────────────────────────────────────────────────────
    {"category_id": "transport.public_transport",
     "category_l1": "transport",     "category_l2": "public_transport",
     "description": "Transporte público: metro, cercanías, autobús, Renfe, Glovo (delivery)"},
    {"category_id": "transport.taxi",
     "category_l1": "transport",     "category_l2": "taxi",
     "description": "Taxis y VTC: FreeNow, Cabify, Uber, licencias de taxi individuales"},

    # ── Ocio y entretenimiento ────────────────────────────────────────────
    {"category_id": "entertainment.restaurant",
     "category_l1": "entertainment", "category_l2": "restaurant",
     "description": "Restaurantes, bares, cafeterías y establecimientos de hostelería"},
    {"category_id": "entertainment.clubs",
     "category_l1": "entertainment", "category_l2": "club",
     "description": "Clubes deportivos, sociales o nocturnos: gimnasios, clubs de golf, discotecas"},
    {"category_id": "entertainment.culture",
     "category_l1": "entertainment", "category_l2": "culture",
     "description": "Cine, teatro, conciertos, museos, libros, música y eventos culturales"},

    # ── Finanzas ──────────────────────────────────────────────────────────
    {"category_id": "finance.credit_card",
     "category_l1": "finance",       "category_l2": "credit_card",
     "description": "Adeudos de liquidación de tarjeta de crédito (Amex, Visa, Mastercard)"},
    {"category_id": "finance.credit_card_debt",
     "category_l1": "finance",       "category_l2": "credit_card_debt",
     "description": "Adeudo mensual acumulado de tarjeta: cargo del total o pago mínimo"},
    {"category_id": "finance.subscriptions",
     "category_l1": "finance",       "category_l2": "subscriptions",
     "description": "Suscripciones digitales periódicas: Netflix, Spotify, OpenAI, Adobe, Amazon Prime"},
    {"category_id": "finance.cash_withdrawal",
     "category_l1": "finance",       "category_l2": "cash_withdrawal",
     "description": "Disposición de efectivo en cajero automático (ATM)"},
    {"category_id": "finance.fees",
     "category_l1": "finance",       "category_l2": "fees",
     "description": "Comisiones bancarias, gastos de servicio y tarifas por operaciones"},
    {"category_id": "finance.charges",
     "category_l1": "finance",       "category_l2": "charges",
     "description": "Cargos genéricos a cuenta: adeudos a su cargo sin categoría específica"},
    {"category_id": "finance.transfer",
     "category_l1": "finance",       "category_l2": "transfer",
     "description": "Transferencias y traspasos entre cuentas propias o a terceros"},
    {"category_id": "finance.loan_payment",
     "category_l1": "finance",       "category_l2": "loan_payment",
     "description": "Amortización de préstamos personales o líneas de crédito"},
    {"category_id": "finance.mortgage_payment",
     "category_l1": "finance",       "category_l2": "mortgage_payment",
     "description": "Cuota mensual de hipoteca o amortización de préstamo hipotecario"},
    {"category_id": "finance.bonus",
     "category_l1": "finance",       "category_l2": "bonus",
     "description": "Bonificaciones, devoluciones e ingresos financieros puntuales"},

    # ── Ingresos ──────────────────────────────────────────────────────────
    {"category_id": "income.salary",
     "category_l1": "income",        "category_l2": "salary",
     "description": "Nómina, salario e ingresos recurrentes por trabajo por cuenta ajena"},

    # ── Servicios ─────────────────────────────────────────────────────────
    {"category_id": "services.misc",
     "category_l1": "services",      "category_l2": "misc",
     "description": "Servicios varios: Bizum, pagos digitales P2P, servicios sin categoría clara"},

    # ── Seguros ───────────────────────────────────────────────────────────
    {"category_id": "insurance.general",
     "category_l1": "insurance",     "category_l2": "general",
     "description": "Seguros generales: hogar, vida, vehículo, salud"},

    # ── Compras ───────────────────────────────────────────────────────────
    {"category_id": "shopping.department_store",
     "category_l1": "shopping",      "category_l2": "department_store",
     "description": "Grandes almacenes y tiendas generales: El Corte Inglés, Zara, H&M, Primark"},
    {"category_id": "shopping.home_furnishings",
     "category_l1": "shopping",      "category_l2": "home_furnishings",
     "description": "Muebles, decoración y artículos del hogar: IKEA, Leroy Merlin, Casa"},
    {"category_id": "shopping.amazon",
     "category_l1": "shopping",      "category_l2": "amazon",
     "description": "Compras generales en Amazon Marketplace (no alimentación ni Prime)"},

    # ── Revisión ──────────────────────────────────────────────────────────
    {"category_id": "needs_review.unknown",
     "category_l1": "needs_review",  "category_l2": "unknown",
     "description": "Pendiente de revisión manual: no encaja con suficiente confianza en ninguna categoría"},
]

# Set de IDs válidos para validación rápida
_VALID_CATEGORY_IDS = {c["category_id"] for c in ALLOWED_CATEGORIES}

# Meses en español para parseo de fechas
SPANISH_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5,  "junio": 6,  "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# ===========================================================================
# ─── PROMPTS LLM ────────────────────────────────────────────────────────────
# ===========================================================================

SYSTEM_PROMPT_BATCH = """
Eres un clasificador de movimientos bancarios españoles.

Recibirás varios movimientos junto con candidatos similares del catálogo y la
lista de categorías válidas. Clasifica cada movimiento devolviendo SOLO un
array JSON válido.

Reglas:
1. Usa SOLO category_id de la lista de categorías válidas proporcionada.
2. No inventes categorías nuevas.
3. Si ninguna categoría encaja con suficiente confianza usa needs_review.unknown
   y pon requires_review = true.
4. confidence es un número entre 0.0 y 1.0.
5. Devuelve SOLO el array JSON, sin texto adicional ni envoltura de objeto.

""".strip()

SYSTEM_PROMPT_SINGLE = """
Eres un clasificador de movimientos bancarios españoles.

Clasifica el movimiento y devuelve SOLO un JSON válido con estas claves:
- category_id    (de la lista de categorías válidas)
- category_l1
- category_l2
- confidence     (0.0 - 1.0)
- requires_review (true si no estás seguro)

Reglas:
1. Usa SOLO category_id de la taxonomía oficial.
2. No inventes categorías nuevas.
3. Si no encaja nada: category_id = needs_review.unknown, requires_review = true.
4. Devuelve SOLO JSON, sin texto adicional.
""".strip()

# ===========================================================================
# ─── CATÁLOGO DE NORMALIZACIÓN (reglas para Qdrant) ─────────────────────────
# ===========================================================================
# Cada entrada mapea aliases de texto observados en PDFs BBVA a una categoría.
# El campo "aliases" son los conceptos reales tal como aparecen en los extractos.
# Este catálogo se indexa en Qdrant como vectores para búsqueda semántica.

NORMALIZATION_CATALOG = [
    {
        "id": "castellana_sports_club",
        "category_id": "entertainment.clubs", "category_l1": "entertainment", "category_l2": "club",
        "aliases": ["castellana sports club"],
    },
    {
        "id": "rodilla",
        "category_id": "entertainment.restaurant", "category_l1": "entertainment", "category_l2": "restaurant",
        "aliases": ["rodilla"],
    },
    {
        "id": "cerveceria_de_pepe",
        "category_id": "entertainment.restaurant", "category_l1": "entertainment", "category_l2": "restaurant",
        "aliases": ["cerveceria de pepe"],
    },
    {
        "id": "restaurantes y cafeterias",
        "category_id": "entertainment.restaurant", "category_l1": "entertainment", "category_l2": "restaurant",
        "aliases": ["PAGO CON TARJETA EN RESTAURANTES Y CAFETERIAS"],
    },
    {
        "id": "el_esquinazo_pizza_bar",
        "category_id": "entertainment.restaurant", "category_l1": "entertainment", "category_l2": "restaurant",
        "aliases": ["el esquinazo pizza bar"],
    },
    {
        "id": "cash_withdrawal",
        "category_id": "finance.cash_withdrawal", "category_l1": "finance", "category_l2": "cash_withdrawal",
        "aliases": [
            "disposicion de efectivo en cajero",
            "disposicion de efectivo en cajero servired",
            "autoserv.bbva madrid - pl. lavm",
            "autoservicio bbva",
        ],
    },
    {
        "id": "charges",
        "category_id": "finance.charges", "category_l1": "finance", "category_l2": "charges",
        "aliases": ["adeudo a su cargo"],
    },
    {
        "id": "american_express",
        "category_id": "finance.credit_card_debt", "category_l1": "finance", "category_l2": "credit_card_debt",
        "aliases": ["adeudo de american express", "american express card de espana sa", "amex"],
    },
    {
        "id": "credit_card_debt",
        "category_id": "finance.credit_card_debt", "category_l1": "finance", "category_l2": "credit_card_debt",
        "aliases": ["adeudo mensual de tarjeta"],
    },
    {
        "id": "atm_fee",
        "category_id": "finance.fees", "category_l1": "finance", "category_l2": "fees",
        "aliases": [
            "comision por operacion en cajero",
            "operacion tarjeta virtual/recarga",
            "comisiones por servicios",
            "comis. disp. cajero",
        ],
    },
    {
        "id": "change_fee",
        "category_id": "finance.fees", "category_l1": "finance", "category_l2": "subscriptions",
        "aliases": ["openai *chatgpt subscr estados unidos de america", "amazon prime"],
    },
    {
        "id": "mortgage_payment",
        "category_id": "finance.mortgage_payment", "category_l1": "finance", "category_l2": "mortgage_payment",
        "aliases": ["cargo por amortizacion de prestamo/credito", "contrato de prestamo", "credito hipotecario"],
    },
    {
        "id": "transfer",
        "category_id": "finance.transfer", "category_l1": "finance", "category_l2": "transfer",
        "aliases": [
            "traspaso",
            "transferencias",
            "abono por transferencia a su favor recibida en euros",
            "bizum"
        ],
    },
    {
        "id": "salary",
        "category_id": "salary.income", "category_l1": "salary", "category_l2": "income",
        "aliases": [
            "abono de nomina por transferencia"
        ],
    },
    {
        "id": "amazon_fresh",
        "category_id": "groceries.amazon", "category_l1": "groceries", "category_l2": "amazon",
        "aliases": ["amazon ufg*hf00i3284", "amazon ufg*hv7hs4o74", "amazon ufg*pr06p34c5"],
    },
    {
        "id": "dulcelandia",
        "category_id": "groceries.supermarket", "category_l1": "groceries", "category_l2": "supermarket",
        "aliases": ["dulcelandia", "supermercados dulcelandia"],
    },
    {
        "id": "comunidad",
        "category_id": "housing.community_fees", "category_l1": "housing", "category_l2": "community_fees",
        "aliases": ["adeudo de comunidad de propietarios y ve", "adeudo a su cargo n ****** alameda del senorio r4", 
        "comunidad de propietarios de san cosme", "comunidad de propiestarios san cosme y dan damian"],
    },
    {
        "id": "salary",
        "category_id": "income.salary", "category_l1": "income", "category_l2": "salary",
        "aliases": ["abono de nomina por transferencia", "ingreso en efectivo", "nomina"],
    },
    {
        "id": "amazon_store",
        "category_id": "shopping.amazon", "category_l1": "shopping", "category_l2": "amazon",
        "aliases": ["amzn mktp es*l81o03th5 luxemburgo", "amzn mktp es*ra8pf5tn5 luxemburgo", "www.amazon.es"],
    },
    {
        "id": "department_store",
        "category_id": "shopping.department_store", "category_l1": "shopping", "category_l2": "department_store",
        "aliases": ["pago con tarjeta en grandes superficies"],
    },
    {
        "id": "public_transport",
        "category_id": "transport.public_transport", "category_l1": "transport", "category_l2": "public_transport",
        "aliases": [
            "freenow* b0hxo3-2", "freenow* axrkdq-2", "freenow* azlwxu-2",
            "freenow* awkn09-2", "freenow* cb0xpu-2", "freenow* cbtrou-2", "freenow* cc83zp-2",
            "free2move* nr012108338", "pisampo",
            "renfe cercanias tna 28 1", "tna. metro de madrid", "metro madrid"
        ],
    },
        {
        "id": "services_misc",
        "category_id": "services.misc", "category_l1": "services", "category_l2": "misc",
        "aliases": ["glovo 04jun m9t1szgj"],
    },
    {
        "id": "taxi",
        "category_id": "transport.taxi", "category_l1": "transport", "category_l2": "taxi",
        "aliases": ["taxi licencia 7980", "licencia 8392", "licencia 02467"],
    },
    {
        "id": "gas_natural",
        "category_id": "utilities.energy", "category_l1": "utilities", "category_l2": "energy",
        "aliases": [
            "adeudo de gas", "gas natural servicios s.a", "gas natural s.u.r. sdg, s.a.",
            "gas natural servicios",
        ],
    },
    {
        "id": "agua",
        "category_id": "utilities.water", "category_l1": "utilities", "category_l2": "water",
        "aliases": [
            "adeudo a su cargo n aqualia gestion integral del agua","ullastres factura de agua"
        ],
    },
    {
        "id": "telefonica",
        "category_id": "utilities.telecommunications", "category_l1": "utilities", "category_l2": "telecommunications",
        "aliases": [
            "adeudo de telecomunicaciones e internet",
            "telefonica moviles", "telefonica moviles s.a.", "movistar","ADEUDO A SU CARGO N ****** TELEFONICA MOVILES S.A."
        ],
    },
    {
        "id": "aquagest",
        "category_id": "utilities.water", "category_l1": "utilities", "category_l2": "water",
        "aliases": ["aqualia gestion integral del agua, s.a.", "aqualia", "aquagest"],
    },
]
# ===========================================================================
# ─── SPARK SCHEMAS ──────────────────────────────────────────────────────────
# ===========================================================================

TARGET_SCHEMA = StructType([
    StructField("txn_id",         StringType(),    True),
    StructField("bank",           StringType(),    True),
    StructField("source_type",    StringType(),    True),
    StructField("period",         StringType(),    True),
    StructField("statement_date", DateType(),      True),
    StructField("txn_date",       DateType(),      True),
    StructField("value_date",     DateType(),      True),
    StructField("concept",        StringType(),    True),
    StructField("concept_norm",   StringType(),    True),
                                                   
    StructField("merchant_key",   StringType(),    True),
    StructField("location",       StringType(),    True),
    StructField("currency",       StringType(),    True),
    StructField("amount",         DoubleType(),    True),
    StructField("amount_abs",     DoubleType(),    True),
    StructField("balance",        DoubleType(),    True),
    StructField("source_pdf",     StringType(),    True),
    StructField("source_path",    StringType(),    True),
    StructField("page",           IntegerType(),   True),
    StructField("row_idx",        IntegerType(),   True),
    StructField("raw",            StringType(),    True),
    #StructField("ingested_at",    TimestampNTZType(), True),
    StructField("ingested_at",    TimestampType(), True),
    StructField("batch_id",       StringType(),    True),
])

# Schema simplificado: solo lo que necesitamos
ENRICH_SCHEMA = StructType([
    StructField("txn_id",              StringType(),    False),
    StructField("merchant_norm",       StringType(),    True),
    StructField("canonical_label",     StringType(),    True),
    StructField("category_l1",         StringType(),    True),
    StructField("category_l2",         StringType(),    True),
    StructField("category_id",         StringType(),    True),
    StructField("category_path",       StringType(),    True),
    StructField("confidence",          DoubleType(),    True),
    StructField("validation_status",   StringType(),    True),
    StructField("requires_review",     BooleanType(),   True),
    StructField("catalog_hash",        StringType(),    True),
    StructField("taxonomy_source",     StringType(),    True),
    StructField("taxonomy_table",      StringType(),    True),
    StructField("method",              StringType(),    True),
    StructField("model",               StringType(),    True),
    StructField("embed_model",         StringType(),    True),
    StructField("catalog_version",     StringType(),    True),
    StructField("prompt_hash",         StringType(),    True),
    StructField("retrieval_top_k",     IntegerType(),   True),
    StructField("chosen_candidate_id", StringType(),    True),
    StructField("candidates_json",     StringType(),    True),
    StructField("enrichment_json",     StringType(),    True),
    StructField("enriched_at",         TimestampType(), True),
    #StructField("ingested_at",        TimestampNTZType(), True),
    StructField("batch_id",            StringType(),    True),
])

# ===========================================================================
# ─── DATACLASSES ────────────────────────────────────────────────────────────
# ===========================================================================

@dataclass
class PageLog:
    page:        int
    page_type:   str   # "ACCOUNT_STATEMENT" | "CARD_STATEMENT" | "NONE"
    text_chars:  int
    account_rows: int
    card_rows:   int


@dataclass
class ExtractionResult:
    pdf:         str
    pages_total: int
    pages_log:   List[PageLog]

# ===========================================================================
# ─── REGEX PARA PARSEO DE PDF ───────────────────────────────────────────────
# ===========================================================================

DDMM     = r"\d{2}/\d{2}"
DDMMYYYY = r"\d{2}/\d{2}/\d{4}"
AMOUNT   = r"[-(]?\d{1,3}(?:\.\d{3})*,\d{2}[)]?"
CUR      = r"(?:EUR|€)"

ACCOUNT_ROW_RE = re.compile(
    rf"^\s*({DDMM})\s+({DDMM})\s+(.*?)\s+({AMOUNT})\s+({AMOUNT})(?:\s+({CUR}))?\s*$",
    re.IGNORECASE,
)
CARD_ROW_RE = re.compile(
    rf"\b({DDMMYYYY})\b.*?\b([A-ZÁÉÍÓÚÑ ]{{2,}})\b\s+({AMOUNT})\s*$",
    re.IGNORECASE,
)
ACCOUNT_TAIL_RE = re.compile(
    rf"^\s*({AMOUNT})\s+({AMOUNT})(?:\s+({CUR}))?\s*$",
    re.IGNORECASE,
)

# NUEVOS: parser secuencial de cuenta
ACCOUNT_DATE_ONLY_RE = re.compile(rf"^\s*({DDMM})\s*$", re.IGNORECASE)
ACCOUNT_HEAD_INLINE_RE = re.compile(
    rf"^\s*({DDMM})(?:\s+({DDMM}))?\s+(.*)$",
    re.IGNORECASE,
)
ACCOUNT_AMOUNT_ONLY_RE = re.compile(rf"^\s*({AMOUNT})\s*$", re.IGNORECASE)
ACCOUNT_CUR_ONLY_RE = re.compile(rf"^\s*({CUR})\s*$", re.IGNORECASE)

DATE_DDMM_RE     = re.compile(rf"\b{DDMM}\b")
DATE_DDMMYYYY_RE = re.compile(rf"\b{DDMMYYYY}\b")
AMOUNT_RE        = re.compile(AMOUNT)
CUR_RE           = re.compile(rf"\b{CUR}\b", re.IGNORECASE)

# OJO: segunda fecha opcional
ACCOUNT_START_RE = re.compile(rf"^\s*{DDMM}(?:\s+{DDMM})?\b", re.IGNORECASE)

SPLIT_COLS       = re.compile(r"\s{2,}")
CARD_DATE_START  = re.compile(r"^\s*(\d{2}/\d{2}/\d{4})\b")
AMOUNT_ES_RE     = re.compile(r"[-(]?\d{1,3}(?:\.\d{3})*,\d{2}[)]?")
NO_DATE_ROW_WITH_AMOUNT_RE = re.compile(r".*\b(" + AMOUNT + r")\s*$", re.IGNORECASE)
UPPER_WORDS      = re.compile(r"^[A-ZÁÉÍÓÚÑ ]{3,}$")

ACCOUNT_STOP_RE = re.compile(
    r"^\s*(saldo\b|saldo fin de mes\b|avance de movimientos\b|"
    r"movimientos de tarjeta\b|atenci[oó]n\b|a continuaci[oó]n\b|"
    r"elena de la cruz\b|informe mensual\b)",
    re.IGNORECASE,
)
JUNK_PATTERNS = [
    r"\binforme mensual\b",
    r"\b\d+\s*de\s*\d+\b",
    r"\batención\b",
    r"\bbbva\.es\b",
    r"\bgoogle play\b|\bapp store\b",
    r"\bdescarga la app\b|\bapp bbva\b",
    r"^¿",
    r"\b¿sabes que\b",
    r"\bventajas\b|\bconsulta condiciones\b",
    r"^\d{12,}$",
    r"^[-_]{5,}$",
    r"^cargos\s+abonos$",
    r"^fecha\s+concepto\s+localidad",
    r"^\s*total\b",
    r"^\s*tarjeta\b",
    r"^\s*fecha\s+concepto\b",
    r"^\(\d+\)",
    r"\bcircular del banco de espa[nñ]a\b",
    r"\bquieres seguir comprando\b",
    r"\bmantener tu saldo al mismo nivel\b",
    r"\binformate en l[ií]nea bbva\b",
    r"\bcualquier oficina bbva\b",
    r"\bdescubre bbva wallet\b",
    r"\bpago que te ayudan a pagar comodamente\b",
    r"\bnuevo saldo\b",
    r"\bl[ií]mite de cr[eé]dito\b",
    r"\bforma de pago\b",
    r"\bimporte a pagar\b",
    r"\bintereses/comisiones\b",
    r"\bsaldo anterior\b",
    r"\brecibo anterior\b",
    r"\boperaciones mes\b",
    r"\bexceso l[ií]mite\b",
    r"\bsaldo pendiente\b",
]
JUNK_RE = re.compile("|".join(f"(?:{p})" for p in JUNK_PATTERNS), re.IGNORECASE)

# ===========================================================================
# ─── HELPERS GENERALES ──────────────────────────────────────────────────────
# ===========================================================================

def safe_float(value, default: float = 0.0) -> Optional[float]:
    try:
        return float(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def norm_text(s) -> Optional[str]:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    s = str(s).lower().strip()
    s = strip_accents(s)
    s = re.sub(r"\s+", " ", s)
    return s or None


def is_junk_line(s: str) -> bool:
    return bool(JUNK_RE.search(s.strip()))


def build_date_ddmmyyyy(ddmmyyyy: str) -> pd.Timestamp:
    d, m, y = ddmmyyyy.split("/")
    return pd.Timestamp(int(y), int(m), int(d))


def infer_statement_date_from_text(text: str) -> pd.Timestamp:
    pattern = re.compile(
        r"(?:informe\s+mensual|extracto|periodo)[^\n]*"
        r"(\d{1,2})\s+de\s+(\w+)\s+(?:de\s+)?(\d{4})",
        re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        day, month_str, year = m.group(1), m.group(2).lower(), m.group(3)
        month = SPANISH_MONTHS.get(month_str)
        if month:
            return pd.Timestamp(int(year), month, int(day))
    dates = DATE_DDMMYYYY_RE.findall(text)
    if dates:
        return build_date_ddmmyyyy(dates[0])
    return pd.Timestamp.now().normalize()


def build_date_ddmm_with_statement_context(
    ddmm: str, statement_date: pd.Timestamp
) -> pd.Timestamp:
    d, m = ddmm.split("/")
    year = statement_date.year
    try:
        ts = pd.Timestamp(year, int(m), int(d))
    except ValueError:
        ts = pd.Timestamp(year - 1, int(m), int(d))
    if abs((ts - statement_date).days) > 200:
        ts = pd.Timestamp(year - 1 if ts > statement_date else year + 1, int(m), int(d))
    return ts


def make_batch_id(pdf_path: str) -> str:
    base = os.path.basename(pdf_path).replace(".pdf", "")
    ts   = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    return f"{base}_{ts}"


def make_txn_id(row) -> str:
    key = "|".join(str(row.get(f, "")) for f in [
        "bank", "source_type", "txn_date", "value_date",
        "concept_norm", "amount", "source_pdf",
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:24]

# ===========================================================================
# ─── S3 / CONTROL DE IDEMPOTENCIA ───────────────────────────────────────────
# ===========================================================================

def put_json_to_s3(s3, bucket: str, key: str, payload: dict) -> None:
    s3.put_object(
        Bucket=bucket, Key=key,
        Body=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
        ContentType="application/json",
    )


def list_keys(s3, bucket: str, prefix: str) -> List[str]:
    keys, paginator = [], s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    return keys


def build_processed_control_key(file_hash: str) -> str:
    return f"{CONTROL_PROCESSED_PREFIX}/{file_hash}.json"


def build_failed_control_key(file_hash: str) -> str:
    return f"{CONTROL_FAILED_PREFIX}/{file_hash}.json"


def was_pdf_already_processed_by_control(s3, bucket: str, file_hash: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=build_processed_control_key(file_hash))
        return True
    except Exception:
        return False


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def list_landing_pdfs(s3, bucket: str, prefix: str) -> List[str]:
    all_keys      = list_keys(s3, bucket, prefix)
    control_keys  = set(list_keys(s3, bucket, CONTROL_PROCESSED_PREFIX))
    pdf_keys      = [k for k in all_keys if k.lower().endswith(".pdf")]
    result        = []
    for key in pdf_keys:
        fname     = os.path.basename(key).replace(".pdf", "")
        already   = any(fname in ck for ck in control_keys)
        if not already:
            result.append(key)
    return result


def download_pdf_to_tempfile(s3, bucket: str, key: str) -> str:
    suffix = "_" + os.path.basename(key)
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    s3.download_file(bucket, key, path)
    return path


def read_vault_secret(vault_addr: str, vault_token: str, secret_path: str, key: str) -> str:
    url     = f"{vault_addr}/v1/{secret_path}"
    headers = {"X-Vault-Token": vault_token}
    r       = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()
    secret = data.get("data", {}).get(key) or data.get("data", {}).get("data", {}).get(key)
    if not secret:
        raise ValueError(f"Secret key '{key}' not found at '{secret_path}'")
    return secret

# ===========================================================================
# ─── POPPLER / EXTRACCIÓN DE PDF ────────────────────────────────────────────
# ===========================================================================

def check_poppler() -> None:
    for tool in ("pdftotext", "pdfinfo"):
        if not shutil.which(tool):
            raise RuntimeError(f"Poppler tool not found in PATH: {tool}")


def _looks_encrypted(stderr: str) -> bool:
    """
    Devuelve True SOLO si stderr indica cifrado incorrecto o falta de contraseña.

    NO incluir "error" ni "no text" — generan falsos positivos con PDFs válidos
    que producen warnings de Poppler como "Syntax Warning: Invalid Font" o
    "Error: PDF..." aunque la contraseña sea correcta y returncode sea 0.
    """
    s = stderr.lower()
    return any(k in s for k in (
        "incorrect password",
        "command line error: incorrect password",
        "encrypted",
        "password required",
        "wrong password",
        "permission denied",
    ))


def _run_pdftotext(
    pdf_path: str, page_num_1based: int, password: Optional[str]
) -> subprocess.CompletedProcess:
    cmd = ["pdftotext", "-layout", "-f", str(page_num_1based), "-l", str(page_num_1based)]
    if password:
        cmd += ["-upw", password, "-opw", password]
    cmd += [pdf_path, "-"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def pdftotext_page(
    pdf_path: str, page_num_1based: int, password: Optional[str] = None
) -> str:
    result = _run_pdftotext(pdf_path, page_num_1based, password)
    if result.returncode != 0 and _looks_encrypted(result.stderr):
        raise ValueError(f"PDF encrypted or wrong password: {result.stderr[:200]}")
    return result.stdout


def _run_pdfinfo(
    pdf_path: str, password: Optional[str], flag: Optional[str]
) -> subprocess.CompletedProcess:
    cmd = ["pdfinfo"]
    if flag:
        cmd.append(flag)
    if password:
        cmd += ["-upw", password, "-opw", password]
    cmd.append(pdf_path)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15)


def get_pdf_page_count(pdf_path: str, password: Optional[str] = None) -> int:
    result = _run_pdfinfo(pdf_path, password, None)
    for line in result.stdout.splitlines():
        if line.lower().startswith("pages:"):
            return int(line.split(":")[1].strip())
    return 1


def resolve_pdf_password(pdf_path: str, base_password: str) -> Optional[str]:
    """
    Prueba dos contraseñas para abrir un PDF BBVA:
      1. La contraseña exacta obtenida de Vault.
      2. Fallback: '0' + contraseña.upper()  (ej: '53429593vh' -> '053429593VH')
         Necesario cuando el NIF tiene 8 dígitos y BBVA lo almacenó con padding y mayúsculas.
    Si ninguna funciona lanza ValueError con el stderr de Poppler.
    """
    candidates = [
        base_password,
        "0" + base_password.upper(),
    ]
    for pwd in candidates:
        result = _run_pdftotext(pdf_path, 1, pwd)
        if result.returncode == 0 and not _looks_encrypted(result.stderr):
            #print(f"  [pdf] Abierto con contraseña='{pwd}'")
            return pwd
    last_stderr = _run_pdftotext(pdf_path, 1, base_password).stderr[:300]
    raise ValueError(
        f"No se pudo abrir el PDF. Probadas: {candidates}. "
        f"Poppler stderr: {last_stderr}"
    )
# ===========================================================================
# ─── PARSEO DE TRANSACCIONES ────────────────────────────────────────────────
# ===========================================================================

def classify_page(lines: List[str]) -> str:
    CARD_DATE_START_LOCAL = re.compile(r"^\s*\d{2}/\d{2}/\d{4}\b")
    sample = "\n".join(lines[:250])

    account_hits = sum(1 for l in lines if ACCOUNT_ROW_RE.search(l))
    card_hits = sum(1 for l in lines if CARD_DATE_START_LOCAL.match(l.strip()))

    if card_hits >= 3:
        return "CARD_STATEMENT"

    ddmm = len(DATE_DDMM_RE.findall(sample))
    ddmmyyyy = len(DATE_DDMMYYYY_RE.findall(sample))
    cur = len(CUR_RE.findall(sample))
    amounts = len(AMOUNT_RE.findall(sample))

    if account_hits >= 2:
        return "ACCOUNT_STATEMENT"
    if card_hits >= 2:
        return "CARD_STATEMENT"
    if amounts >= 5 and ddmmyyyy >= 5:
        return "CARD_STATEMENT"
    if amounts >= 5 and ddmm >= 10 and cur >= 2:
        return "ACCOUNT_STATEMENT"

    return "NONE"


def merge_continuations(lines: List[str]) -> List[str]:
    merged, buf = [], None
    for line in lines:
        if ACCOUNT_START_RE.match(line):
            if buf:
                merged.append(buf)
            buf = line
        elif buf and not is_junk_line(line) and line.strip() and not DATE_DDMMYYYY_RE.search(line):
            buf += " " + line.strip()
        else:
            if buf:
                merged.append(buf)
                buf = None
            if line.strip():
                merged.append(line)
    if buf:
        merged.append(buf)
    return merged


def normalize_amount_es(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    s = s.strip().replace("(", "-").replace(")", "")
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_account_line(line: str) -> Optional[dict]:
    m = re.match(
        rf"^\s*({DDMM})\s+({DDMM})\s+(.*?)\s+({AMOUNT})\s+({AMOUNT})(?:\s+({CUR}))?\s*$",
        line,
        re.IGNORECASE,
    )
    if not m:
        return None

    txn_ddmm = m.group(1)
    val_ddmm = m.group(2)
    concept_raw = (m.group(3) or "").strip()
    amt_str = m.group(4)
    bal_str = m.group(5)
    cur_str = m.group(6) or "EUR"

    location = None
    parts = SPLIT_COLS.split(concept_raw)
    if len(parts) > 1 and UPPER_WORDS.fullmatch(parts[-1].strip()):
        location = parts[-1].strip()
        concept_raw = " ".join(parts[:-1]).strip()

    return {
        "txn_ddmm": txn_ddmm,
        "val_ddmm": val_ddmm,
        "concept": concept_raw,
        "location": location,
        "amount": normalize_amount_es(amt_str),
        "balance": normalize_amount_es(bal_str),
        "currency": cur_str.replace("€", "EUR").upper(),
        "raw": line.strip(),
    }
    
def _finalize_account_row(current: dict) -> Optional[dict]:
    if not current:
        return None
    if not current.get("txn_ddmm"):
        return None
    if not current.get("val_ddmm"):
        current["val_ddmm"] = current["txn_ddmm"]
    if current.get("amount") is None:
        return None
    if current.get("balance") is None:
        return None

    concept_lines = current.get("concept_lines", []) or []
    concept_raw = " ".join(concept_lines).strip()
    concept_raw = re.sub(r"\s+", " ", concept_raw).strip()
    if not concept_raw:
        return None

    concept_main = re.sub(r"\s+", " ", concept_lines[0]).strip() if concept_lines else concept_raw

    location = None
    cols = SPLIT_COLS.split(concept_raw)
    if len(cols) > 1 and UPPER_WORDS.fullmatch(cols[-1].strip()):
        location = cols[-1].strip()
        concept_raw = " ".join(cols[:-1]).strip()

    raw_parts = current.get("raw_parts", [])
    return {
        "txn_ddmm": current["txn_ddmm"],
        "val_ddmm": current["val_ddmm"],
        "concept_main": concept_main,
        "concept": concept_raw,
        "location": location,
        "amount": current["amount"],
        "balance": current["balance"],
        "currency": (current.get("currency") or "EUR").replace("€", "EUR").upper(),
        "raw": " | ".join(raw_parts),
    }

def parse_account_block(lines: List[str]) -> List[dict]:
    rows: List[dict] = []
    current: Optional[dict] = None
    pending_txn_date: Optional[str] = None

    def flush():
        nonlocal current
        row = _finalize_account_row(current)
        if row is not None:
            rows.append(row)
        current = None

    for raw in lines:
        s = (raw or "").strip()
        if not s:
            continue

        if ACCOUNT_STOP_RE.match(s):
            flush()
            pending_txn_date = None
            continue

        if is_junk_line(s):
            continue

        # 1) línea que empieza por fecha(s)
        m_inline = ACCOUNT_HEAD_INLINE_RE.match(s)
        if m_inline:
            # antes de arrancar un movimiento nuevo, cerrar el anterior
            flush()
            pending_txn_date = None

            parsed_inline = parse_account_line(s)

            # CASO A: la línea ya trae importe + saldo
            # NO se añade directamente a rows; se deja abierta para posibles continuaciones
            if parsed_inline:
                current = {
                    "txn_ddmm": parsed_inline["txn_ddmm"],
                    "val_ddmm": parsed_inline["val_ddmm"],
                    "concept_lines": [parsed_inline["concept"]],
                    "amount": parsed_inline["amount"],
                    "balance": parsed_inline["balance"],
                    "currency": parsed_inline["currency"],
                    "raw_parts": [s],
                }
                continue

            # CASO B: línea parcial, seguir acumulando
            current = {
                "txn_ddmm": m_inline.group(1),
                "val_ddmm": m_inline.group(2) or m_inline.group(1),
                "concept_lines": [m_inline.group(3).strip()],
                "amount": None,
                "balance": None,
                "currency": None,
                "raw_parts": [s],
            }
            continue

        # 2) fecha sola
        m_date_only = ACCOUNT_DATE_ONLY_RE.match(s)
        if m_date_only:
            if current and (current.get("amount") is not None or current.get("concept_lines")):
                flush()

            if pending_txn_date is None:
                pending_txn_date = m_date_only.group(1)
            else:
                current = {
                    "txn_ddmm": pending_txn_date,
                    "val_ddmm": m_date_only.group(1),
                    "concept_lines": [],
                    "amount": None,
                    "balance": None,
                    "currency": None,
                    "raw_parts": [pending_txn_date, s],
                }
                pending_txn_date = None
            continue

        # 3) si había fecha pendiente y llega texto
        if pending_txn_date is not None and current is None:
            current = {
                "txn_ddmm": pending_txn_date,
                "val_ddmm": pending_txn_date,
                "concept_lines": [],
                "amount": None,
                "balance": None,
                "currency": None,
                "raw_parts": [pending_txn_date],
            }
            pending_txn_date = None

        if current is None:
            continue

        current["raw_parts"].append(s)

        # 4) cola compacta con importe + saldo
        m_tail = ACCOUNT_TAIL_RE.match(s)
        if m_tail:
            current["amount"] = normalize_amount_es(m_tail.group(1))
            current["balance"] = normalize_amount_es(m_tail.group(2))
            current["currency"] = m_tail.group(3) or "EUR"
            continue

        # 5) importe solo
        m_amt = ACCOUNT_AMOUNT_ONLY_RE.match(s)
        if m_amt:
            value = normalize_amount_es(m_amt.group(1))
            if current["amount"] is None:
                current["amount"] = value
            elif current["balance"] is None:
                current["balance"] = value
            else:
                current["concept_lines"].append(s)
            continue

        # 6) divisa sola
        m_cur = ACCOUNT_CUR_ONLY_RE.match(s)
        if m_cur:
            current["currency"] = m_cur.group(1)
            if current["amount"] is not None and current["balance"] is not None:
                flush()                                                       
            continue

        # 7) texto de continuación
        current["concept_lines"].append(s)

    flush()
    return rows

def group_account_rows(lines: List[str]) -> List[List[str]]:
    # Ya no se usa para parsear cuenta; se deja solo por compatibilidad.
    cleaned = []
    for raw in lines:
        s = (raw or "").rstrip("\n")
        if s.strip():
            cleaned.append(s)
    return [cleaned] if cleaned else []


# ===========================================================================
# ─── PARSER DE TARJETA ROBUSTO (NO DEPENDE DE FECHA EN TODOS LOS REGISTROS)
# ===========================================================================

CARD_TOTAL_RE = re.compile(r"^\s*total\b", re.I)

CARD_HEADER_RE = re.compile(
    r"^\s*(movimientos de tarjeta|contrato de tarjeta|cuenta de cargo|"
    r"l[ií]mite de cr[eé]dito|forma de pago|saldo anterior|recibo anterior|"
    r"operaciones mes|intereses/comisiones|exceso l[ií]mite|atrasos|saldo pendiente|"
    r"importe amort\.?|importe a pagar|nuevo saldo|fecha concepto localidad cargos abonos|"
    r"tarjeta\s+\d)",
    re.I,
)

CARD_FOOTNOTE_RE = re.compile(r"^\(\d+\)\s", re.I)


def _is_card_noise_line(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return True
    if is_junk_line(s):
        return True
    if CARD_HEADER_RE.match(s):
        return True
    if CARD_FOOTNOTE_RE.match(s):
        return True
    return False


def _split_trailing_amount(s: str) -> Tuple[str, Optional[float]]:
    """
    Extrae un importe al final de la línea, si existe.
    Devuelve (texto_sin_importe, importe_float|None)
    """
    m = re.search(rf"\s+({AMOUNT})\s*$", s, re.IGNORECASE)
    if not m:
        return s.strip(), None

    amount_raw = m.group(1)
    text = s[:m.start(1)].strip()
    return text, normalize_amount_es(amount_raw)


def _is_credit_card_movement(concept: str) -> bool:
    return bool(
        re.search(
            r"\b(anul|anulaci[oó]n|abono|devol|refund|reinteg|ingreso)\b",
            (concept or "").lower(),
        )
    )


def _signed_card_amount(concept: str, amount_abs: Optional[float]) -> Optional[float]:
    if amount_abs is None:
        return None
    if _is_credit_card_movement(concept):
        return abs(amount_abs)
    return -abs(amount_abs)


def _split_card_concept_location(text: str) -> Tuple[str, Optional[str]]:
    """
    Mantiene la heurística existente para localidad, pero sin forzarla.
    Si no puede separarla con seguridad, deja location=None.
    """
    text = re.sub(r"\s+", " ", (text or "").strip())

    location = None
    parts = SPLIT_COLS.split(text)
    if len(parts) > 1 and UPPER_WORDS.fullmatch(parts[-1].strip()):
        location = parts[-1].strip()
        text = " ".join(parts[:-1]).strip()

    return text, location


def _make_card_row(
    txn_date_str: str,
    concept_text: str,
    amount_abs: Optional[float],
    raw: str,
) -> dict:
    concept_text = re.sub(r"\s+", " ", (concept_text or "").strip())
    concept_main, location = _split_card_concept_location(concept_text)

    return {
        "txn_date_str": txn_date_str,
        "concept_main": concept_main,
        "concept_parts": [concept_text] if concept_text else [],
        "location": location,
        "amount": _signed_card_amount(concept_main, amount_abs),
        "raw_parts": [raw.strip()],
    }

def parse_card_group(group: List[str]) -> List[dict]:
    return _parse_card_lines_sequential(group)

def parse_card_block(lines: List[str]) -> List[dict]:
    return _parse_card_lines_sequential(lines)


def build_silver_dataframe(
    df_acc: pd.DataFrame,
    df_card: pd.DataFrame,
    pdf_path: str,
    source_path: str,
    raw_text: str,
) -> Optional[pd.DataFrame]:
    statement_date = infer_statement_date_from_text(raw_text)
    batch_id       = make_batch_id(pdf_path)
    source_pdf     = os.path.basename(pdf_path)
    frames         = []

    if df_acc is not None and not df_acc.empty:
        acc = df_acc.copy()
        acc["statement_date"] = statement_date
        acc["txn_date"] = acc["txn_ddmm"].apply(
            lambda x: build_date_ddmm_with_statement_context(x, statement_date)
        )
        acc["value_date"] = acc["val_ddmm"].apply(
            lambda x: build_date_ddmm_with_statement_context(x, statement_date)
        )
        acc["source_type"] = "account"
        acc["currency"] = "EUR"

        concept_norm_source = acc["concept_main"] if "concept_main" in acc.columns else acc["concept"]
        acc["concept_norm"] = concept_norm_source.apply(norm_text)
       
        acc["merchant_key"] = acc["concept_norm"]
        acc["amount_abs"] = acc["amount"].abs()
        acc["balance"] = acc.get("balance", pd.Series(dtype=float))
        acc["page"] = 0
        acc["row_idx"] = range(len(acc))
        acc["raw"] = acc.apply(lambda r: json.dumps(r.to_dict(), default=str), axis=1)

        frames.append(acc[[
            "concept", "concept_norm", "merchant_key", "location",
            "currency", "amount", "amount_abs", "balance",
            "source_type", "statement_date", "txn_date", "value_date",
            "page", "row_idx", "raw",
        ]])

    if df_card is not None and not df_card.empty:
        card = df_card.copy()
        card["statement_date"] = statement_date
        card["txn_date"] = card["txn_date_str"].apply(build_date_ddmmyyyy)
        card["value_date"] = card["txn_date"]
        card["source_type"] = "card"
        card["currency"] = "EUR"

        concept_norm_source = card["concept_main"] if "concept_main" in card.columns else card["concept"]
        card["concept_norm"] = concept_norm_source.apply(norm_text)        

        card["merchant_key"] = card["concept_norm"]
        card["amount_abs"] = card["amount"].abs()
        card["balance"] = None
        card["page"] = 0
        card["row_idx"] = range(len(card))
        card["raw"] = card.apply(lambda r: json.dumps(r.to_dict(), default=str), axis=1)

        frames.append(card[[
            "concept", "concept_norm", "merchant_key", "location",
            "currency", "amount", "amount_abs", "balance",
            "source_type", "statement_date", "txn_date", "value_date",
            "page", "row_idx", "raw",
        ]])

    if not frames:
        return None

    df = pd.concat(frames, ignore_index=True)
    df["bank"] = "bbva"
    df["period"] = statement_date.strftime("%Y-%m")
    df["source_pdf"] = source_pdf
    df["source_path"] = source_path
    df["ingested_at"] = pd.Timestamp.now("UTC")
    df["batch_id"] = batch_id
    df["txn_id"] = df.apply(make_txn_id, axis=1)

    return df


def group_card_rows(lines: List[str]) -> List[List[str]]:
    """
    Se mantiene por compatibilidad, pero ya no agrupa por fecha.
    Devolvemos un único bloque limpio para que el parser secuencial
    gestione fechas heredadas, comisiones sin fecha, etc.
    """
    cleaned = []
    for raw in lines:
        s = (raw or "").rstrip("\n")
        if not s.strip():
            continue
        cleaned.append(s)
    return [cleaned] if cleaned else []

def _parse_card_lines_sequential(lines: List[str]) -> List[dict]:
    rows: List[dict] = []
    current: Optional[dict] = None
    last_date: Optional[str] = None

    def flush_current():
        nonlocal current, rows
        if current and current.get("amount") is not None:
            parts = [re.sub(r"\s+", " ", p).strip() for p in current.get("concept_parts", []) if p and p.strip()]
            if parts:
                concept_full = " ".join(parts).strip()
                concept_main = re.sub(r"\s+", " ", (current.get("concept_main") or parts[0]).strip())
                raw_full = " | ".join(current.get("raw_parts", []))

                rows.append({
                    "txn_date_str": current["txn_date_str"],
                    "concept_main": concept_main,
                    "concept": concept_full,
                    "location": current.get("location"),
                    "amount": current.get("amount"),
                    "raw": raw_full,
                })
        current = None

    for raw in lines:
        s = (raw or "").strip()
        if not s:
            continue

        if _is_card_noise_line(s):
            continue

        if CARD_TOTAL_RE.match(s):
            flush_current()
            break

        dm = CARD_DATE_START.match(s)

        if dm:
            flush_current()

            txn_date_str = dm.group(1)
            last_date = txn_date_str

            rest = s[dm.end():].strip()
            text_part, amount_abs = _split_trailing_amount(rest)

            current = _make_card_row(
                txn_date_str=txn_date_str,
                concept_text=text_part,
                amount_abs=amount_abs,
                raw=s,
            )
            continue

        text_part, amount_abs = _split_trailing_amount(s)

        if amount_abs is not None:
            if current is not None and current.get("amount") is None:
                if text_part:
                    current["concept_parts"].append(text_part)
                current["amount"] = _signed_card_amount(
                    current.get("concept_main") or (current["concept_parts"][0] if current["concept_parts"] else ""),
                    amount_abs,
                )
                current["raw_parts"].append(s)
            else:
                if current is not None:
                    flush_current()

                if last_date is None:
                    continue

                orphan = _make_card_row(
                    txn_date_str=last_date,
                    concept_text=text_part,
                    amount_abs=amount_abs,
                    raw=s,
                )
                rows.append({
                    "txn_date_str": orphan["txn_date_str"],
                    "concept_main": orphan["concept_main"],
                    "concept": " ".join(orphan["concept_parts"]).strip(),
                    "location": orphan.get("location"),
                    "amount": orphan.get("amount"),
                    "raw": " | ".join(orphan["raw_parts"]),
                })
            continue

        if current is not None:
            current["concept_parts"].append(s)
            current["raw_parts"].append(s)
        else:
            if last_date is not None:
                current = _make_card_row(
                    txn_date_str=last_date,
                    concept_text=s,
                    amount_abs=None,
                    raw=s,
                )

    flush_current()
    return rows


def extract_pdf_transactions(
    pdf_path: str,
    password: Optional[str] = None,
    verbose: bool = False,
    show_parse_samples: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame, ExtractionResult, str]:
    n_pages    = get_pdf_page_count(pdf_path, password)
    acc_rows, card_rows = [], []
    pages_log           = []
    all_text            = []

    if verbose:
        print(f"  [pdf] {os.path.basename(pdf_path)}: {n_pages} páginas")

    for page in range(1, n_pages + 1):
        text  = pdftotext_page(pdf_path, page, password)
        lines = [l for l in text.splitlines() if l.strip() and not is_junk_line(l)]
        all_text.append(text)

        page_type = classify_page(lines)
        a_rows = parse_account_block(lines) if page_type == "ACCOUNT_STATEMENT" else []
        c_rows = parse_card_block(lines)    if page_type == "CARD_STATEMENT"    else []

        acc_rows.extend(a_rows)
        card_rows.extend(c_rows)

        pages_log.append(PageLog(
            page=page,
            page_type=page_type,
            text_chars=len(text.replace(" ", "")),
            account_rows=len(a_rows),
            card_rows=len(c_rows),
        ))

        if verbose:
            print(
                f"  [pdf] página {page}/{n_pages}: tipo={page_type} "
                f"chars={len(text.replace(' ',''))} "
                f"líneas={len(lines)} acc={len(a_rows)} card={len(c_rows)}"
            )

        if (
            show_parse_samples
            and len(lines) > 3
            and len(a_rows) == 0
            and len(c_rows) == 0
            and page_type != "NONE"
        ):
            sample = lines[:6]
            print(f"  [pdf] WARN página {page} sin filas parseadas. Muestra de líneas:")
            for l in sample:
                print(f"    | {l[:120]}")

    df_acc  = pd.DataFrame(acc_rows)
    df_card = pd.DataFrame(card_rows)

    if verbose:
        print(f"  [pdf] Total: acc={len(df_acc)} card={len(df_card)}")

    log = ExtractionResult(
        pdf=os.path.basename(pdf_path),
        pages_total=n_pages,
        pages_log=pages_log,
    )
    return df_acc, df_card, log, "\n".join(all_text)

# ===========================================================================
# ─── CONSTRUCCIÓN DEL DATAFRAME SILVER ──────────────────────────────────────
# ===========================================================================

def normalize_silver_for_spark(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce types to match TARGET_SCHEMA before createDataFrame."""
    if df is None or df.empty:
        return df

    df = df.copy()

    expected_cols = [f.name for f in TARGET_SCHEMA.fields]
    for col in expected_cols:
        if col not in df.columns:
            df[col] = None

    for col in ("statement_date", "txn_date", "value_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
            df[col] = df[col].where(pd.notnull(df[col]), None).astype(object)

    if "ingested_at" in df.columns:
        s = pd.to_datetime(df["ingested_at"], errors="coerce", utc=True)
        df["ingested_at"] = s.dt.tz_localize(None).astype("datetime64[us]")

    for col in ("amount", "amount_abs", "balance"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ("page", "row_idx"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")

    str_cols = [f.name for f in TARGET_SCHEMA.fields if isinstance(f.dataType, StringType)]
    for col in str_cols:
        if col in df.columns:
            df[col] = df[col].where(df[col].notna(), None).astype(object)

    return df[expected_cols]

# ===========================================================================
# ─── OLLAMA ─────────────────────────────────────────────────────────────────
# ===========================================================================

def ollama_embed(text: str, model: str = OLLAMA_EMBED_MODEL) -> List[float]:
    r = requests.post(
        f"{OLLAMA_URL}/api/embeddings",
        json={"model": model, "prompt": text},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["embedding"]


def ollama_chat_json(system_prompt: str, user_prompt: str, model: str = OLLAMA_CHAT_MODEL) -> dict:
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": model,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.0},
            "messages": [
                {"role": "system",  "content": system_prompt},
                {"role": "user",    "content": user_prompt},
            ],
        },
        timeout=120,
    )
    r.raise_for_status()
    content = r.json()["message"]["content"]
    return json.loads(content)


def ollama_chat_json_array(
    system_prompt: str, user_prompt: str, model: str = OLLAMA_CHAT_MODEL
) -> List[dict]:
    raw = ollama_chat_json(system_prompt, user_prompt, model)
    def _coerce_item(x):
        if isinstance(x, dict):
            return x
        if isinstance(x, str):
            x = x.strip()
            if not x:
                return None
            try:
                parsed = json.loads(x)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return None
        return None

    # Caso 1: el JSON raíz ya es una lista
    if isinstance(raw, list):
        out = []
        for item in raw:
            obj = _coerce_item(item)
            if obj is not None:
                out.append(obj)
        return out

    # Caso 2: el JSON raíz es un dict con una lista dentro
    if isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, list):
                out = []
                for item in v:
                    obj = _coerce_item(item)
                    if obj is not None:
                        out.append(obj)
                return out
    return []

# ===========================================================================
# ─── QDRANT ─────────────────────────────────────────────────────────────────
# ===========================================================================

def ensure_qdrant_collection(qdrant: QdrantClient) -> None:
    try:
        qdrant.get_collection(QDRANT_COLLECTION)
    except Exception:
        qdrant.create_collection(
            collection_name=QDRANT_COLLECTION,
            vectors_config=qm.VectorParams(size=768, distance=qm.Distance.COSINE),
        )


def build_catalog_entry_text(item: dict) -> str:
    """Texto a embeder para una entrada del catálogo. Incluye aliases para mejor recall."""
    aliases = " | ".join(item.get("aliases", []))
    return (
        f"category_id: {item['category_id']} | "
        f"category_l1: {item['category_l1']} | "
        f"category_l2: {item['category_l2']} | "
        f"aliases: {aliases}"
    )


def load_normalization_catalog(qdrant: QdrantClient, recreate: bool = False) -> None:
    """
    Indexa NORMALIZATION_CATALOG en Qdrant.

    Con recreate=True elimina y recrea la colección (usar en cambios de modelo
    de embeddings o actualizaciones masivas del catálogo).
    Con recreate=False hace upsert idempotente (IDs deterministas por SHA-256).
    """
    if recreate:
        try:
            qdrant.delete_collection(QDRANT_COLLECTION)
        except Exception:
            pass

    ensure_qdrant_collection(qdrant)

    points = []
    for item in NORMALIZATION_CATALOG:
        text     = build_catalog_entry_text(item)
        vec      = ollama_embed(text)
        # ID determinista: primeros 8 bytes del SHA-256 del texto
        point_id = int.from_bytes(
            hashlib.sha256(text.encode()).digest()[:8], "big", signed=False
        )
        payload  = {
            "id":          item["id"],
            "category_id": item["category_id"],
            "category_l1": item["category_l1"],
            "category_l2": item["category_l2"],
            "aliases":     item.get("aliases", []),
            "text":        text,
        }
        points.append(qm.PointStruct(id=point_id, vector=vec, payload=payload))

    qdrant.upsert(collection_name=QDRANT_COLLECTION, points=points)
    print(f"[qdrant] {len(points)} puntos indexados en '{QDRANT_COLLECTION}'")

# ===========================================================================
# ─── RAG: CLASIFICACIÓN DE TRANSACCIONES ────────────────────────────────────
# ===========================================================================

def build_txn_query_text(row: pd.Series) -> str:
    """
    Construye el texto de consulta para búsqueda vectorial en Qdrant.

    Incluye concept, concept_norm, location y amount para maximizar
    la similitud semántica con las entradas del catálogo.
    """
    parts = [
        f"concept: {row.get('concept', '')}",
        f"merchant_norm: {row.get('merchant_norm', '')}",
        f"tipo: {row.get('source_type', '')}",
    ]
    return " | ".join(p for p in parts if p)


def retrieve_candidates(
    row: pd.Series,
    qdrant: QdrantClient,
    top_k: int = RAG_TOP_K,
) -> List[dict]:
    """Recupera los top_k candidatos más similares de Qdrant para una transacción."""
    query_text = build_txn_query_text(row)
    query_vec  = ollama_embed(query_text)
    response   = qdrant.query_points(
        collection_name=QDRANT_COLLECTION,
        query=query_vec,
        limit=top_k,
        with_payload=True,
        with_vectors=False,
    )
    out = []
    for p in (response.points if hasattr(response, "points") else []):
        payload          = dict(p.payload or {})
        payload["score"] = float(p.score) if p.score is not None else 0.0
        out.append(payload)
    return out



# ===========================================================================
# ─── HARD RULES (pre-clasificación determinista) ────────────────────────────
# ===========================================================================
# Reglas aplicadas ANTES del RAG. Si una transacción encaja, se asigna la
# categoría directamente con confidence=1.0 y method="hard_rule", sin llamar
# a Qdrant ni al LLM. El orden importa: la primera regla que encaja gana.
#
# Criterios para añadir una regla aquí vs dejarla al RAG:
#   - El patrón es inequívoco en cualquier contexto (amazon+fresh → siempre groceries)
#   - La clasificación correcta no depende de otros campos de la fila
#   - Ha aparecido con suficiente frecuencia en los extractos BBVA reales

import re as _re

_HARD_RULES: List[Tuple[_re.Pattern, str]] = [

    # ── Amazon (orden importante: más específico primero) ──────────────────
    # amazon fresh / UFG* (Amazon Fresh delivery): groceries
    (_re.compile(r"amazon.*fresh|amz.*fresh|ufg\*|amazon.*ufg", _re.I),
     "groceries.amazon"),
    # Amazon Prime (suscripción): subscriptions
    (_re.compile(r"amazon.*prime|prime.*amazon|amzn.*prime", _re.I),
     "finance.subscriptions"),
    # Resto de Amazon / AMZN: shopping general
    (_re.compile(r"\bamazon\b|\bamzn\b|amazon\.es|amazon\.com", _re.I),
     "shopping.amazon"),
    # ── Transporte taxi / VTC ──────────────────────────────────────────────
    # Aplicaciones y servicios de taxi conocidos
    (_re.compile(
        r"\bmytaxi\b|\bfreenow\b|\bfree now\b|\bcar2go\b|\bc2g\b|\taxi\b"
        r"|\bcabify\b|\buber\b|\bblablacar\b",
        _re.I),
     "transport.taxi"),
    # ── Telecomunicaciones ─────────────────────────────────────────────────────
    (_re.compile(
        r"\btelefonica\b|\btelef[oó]nica\b|\bmovistar\b|\borange\b|\bvirgin\b|\bsimyo\b",
        _re.I),
     "utilities.telecommunications"),
    # ── Agua ──────────────────────────────────────────────────────────────────
    (_re.compile(
        r"\bagua\b|\baqualia\b",
        _re.I),
     "utilities.water"),
    # ── Supermercados / grandes superficies ───────────────────────────────
    (_re.compile(
        r"mercado|\bsuperficie\b|\bhypermarket\b|\bsupermarket\b"
        r"|\bsupermarkt\b|\bsupermarche\b",
        _re.I),
     "shopping.department_store"),
    # ── Muebles y decoración del hogar ────────────────────────────────────
    (_re.compile(
        r"\bmuebles\b|\bdecorac[ií]on\b|\bdecor\b|\bikea\b"
        r"|\bleroy merlin\b|\bel corte ingles.*hogar\b",
        _re.I),
     "shopping.home_furnishings"),
    # ── Cultura y ocio ─────────────────────────────────────────────────────
    (_re.compile(
        r"\bconcierto\b|\blibros\b|\bm[uú]sica\b|\bdiscos\b"
        r"|\bcines\b|\bcinesa\b|\bteatro\b|\bfnac\b|\bel corte ingles.*libros\b",
        _re.I),
     "entertainment.culture"),
    # ── Clubs y asociaciones ───────────────────────────────────────────────
    # Incluye: 'err77', 'la buga del lobo', cualquier concept_norm con 'club'
    (_re.compile(
        r"\berr\s*77\b|buga del lobo\bclub\b",
        _re.I),
     "entertainment.clubs"),
    # ── Finanzas: adeudos y cargos específicos ────────────────────────────
    (_re.compile(r"adeudo a su cargo", _re.I),
     "finance.credit_card_debt"),
    (_re.compile(r"cargo a su cuenta", _re.I),
     "finance.charges"),
    # ── Ingresos: nómina ───────────────────────────────────────────────────
    (_re.compile(r"\bnomina\b|\bn[oó]mina\b", _re.I),
     "income.salary"),
]


def apply_hard_rules(concept_norm: str) -> Optional[str]:
    """
    Aplica las reglas deterministas al concept_norm de una transacción.

    Evalúa cada regla en orden; devuelve el category_id de la primera que
    encaja, o None si ninguna aplica (en cuyo caso se usa el pipeline RAG).

    Args:
        concept_norm: Texto normalizado del concepto de la transacción.

    Returns:
        category_id válido si hay match, None en caso contrario.
    """
    if not concept_norm:
        return None
    for pattern, category_id in _HARD_RULES:
        if pattern.search(concept_norm):
            return category_id
    return None


def _build_categories_block() -> str:
    """
    Formatea la lista de categorías válidas para incluir en el prompt del LLM.

    Incluye la descripción semántica de cada categoría para que el modelo
    entienda qué tipo de transacciones corresponden a cada category_id,
    no solo su nombre técnico.

    Formato por línea:
        - category_id : descripción
    """
    lines = []
    for c in ALLOWED_CATEGORIES:
        if c["category_id"] == "needs_review.unknown":
            continue
        desc = c.get("description", "")
        lines.append(f"- {c['category_id']} : {desc}")
    lines.append(
        "- needs_review.unknown : Usa esta categoría SOLO si ninguna otra encaja "
        "con suficiente confianza. Pon requires_review = true."
    )
    return "\n".join(lines)


def _validate_category(category_id: str) -> str:
    """Devuelve category_id si es válida, o needs_review.unknown si no lo es."""
    return category_id if category_id in _VALID_CATEGORY_IDS else "needs_review.unknown"


def _category_fields(category_id: str) -> Tuple[str, str]:
    """Devuelve (category_l1, category_l2) para un category_id dado."""
    for c in ALLOWED_CATEGORIES:
        if c["category_id"] == category_id:
            return c["category_l1"], c["category_l2"]
    return "needs_review", "unknown"


def _build_single_user_prompt(row: pd.Series, candidates: List[dict]) -> str:
    """Construye el prompt de usuario para clasificación individual (fallback)."""
    candidates_block = "\n".join(
        f"  {i+1}. {c['category_id']} — similitud: {c['score']:.2f} "
        f"(aliases: {', '.join(c.get('aliases', [])[:3])})"
        for i, c in enumerate(candidates[:3])
    )
    sign = "gasto" if float(row.get("amount", 0) or 0) < 0 else "ingreso"
    return f"""Movimiento bancario:
- Concepto original : {row.get('concept', '')}
- Concepto limpio   : {row.get('concept_norm', '')}
- Tipo de cuenta    : {row.get('source_type', '')}
- Importe           : {row.get('amount', '')} EUR ({sign})
- Localización      : {row.get('location') or 'desconocida'}

Candidatos similares del catálogo:
{candidates_block}

Categorías válidas:
{_build_categories_block()}

Devuelve SOLO:
{{"category_id": "...", "category_l1": "...", "category_l2": "...", "confidence": 0.0, "requires_review": false}}"""


def _build_batch_user_prompt(items: List[dict]) -> str:
    """Construye el prompt de usuario para clasificación en batch."""
    categories_block = _build_categories_block()
    movements = []
    for item in items:
        row        = item["row"]
        candidates = item["candidates"]
        sign       = "gasto" if float(row.get("amount", 0) or 0) < 0 else "ingreso"
        cands_str  = "; ".join(
            f"{c['category_id']} ({c['score']:.2f})"
            for c in candidates[:3]
        )
        movements.append({
            "request_id":    item["request_id"],
            "concept":       row.get("concept_norm") or row.get("concept", ""),
            "type":          row.get("source_type", ""),
            "amount":        f"{row.get('amount', '')} EUR ({sign})",
            "location":      row.get("location") or "",
            "candidates":    cands_str,
        })

    return f"""Movimientos a clasificar:
{json.dumps(movements, ensure_ascii=False, indent=2)}

Categorías válidas:
{categories_block}

Para cada movimiento devuelve un objeto JSON con:
- request_id
- category_id
- category_l1
- category_l2
- confidence  (0.0 - 1.0)
- requires_review (true si no estás seguro)

Devuelve SOLO un array JSON. Sin texto adicional."""


def classify_transaction(
    row: pd.Series,
    qdrant: QdrantClient,
) -> dict:
    """
    Clasifica una transacción individual con el pipeline RAG.

    Flujo:
      1. Búsqueda vectorial en Qdrant → candidatos
      2. Si el top-1 supera RAG_DIRECT_HIT_THRESHOLD → clasificación directa
      3. Si no → llamada al LLM con contexto enriquecido
      4. Si el LLM falla → fallback al top-1

    Devuelve dict con: category_id, category_l1, category_l2,
                       confidence, method, requires_review
    """
    candidates = retrieve_candidates(row, qdrant)

    # Fallback: sin candidatos
    if not candidates:
        return {
            "category_id":     "needs_review.unknown",
            "category_l1":     "needs_review",
            "category_l2":     "unknown",
            "confidence":      0.0,
            "method":          "fallback_no_candidates",
            "requires_review": True,
        }

    top1 = candidates[0]

    # RAG direct hit
    if top1["score"] >= RAG_DIRECT_HIT_THRESHOLD:
        cat_id = _validate_category(top1.get("category_id", "needs_review.unknown"))
        l1, l2 = _category_fields(cat_id)
        return {
            "category_id":     cat_id,
            "category_l1":     l1,
            "category_l2":     l2,
            "confidence":      round(top1["score"], 4),
            "method":          "rag_direct",
            "requires_review": False,
        }

    # LLM individual
    try:
        user_prompt = _build_single_user_prompt(row, candidates)
        result      = ollama_chat_json(SYSTEM_PROMPT_SINGLE, user_prompt)
        cat_id      = _validate_category(result.get("category_id", "needs_review.unknown"))
        l1, l2      = _category_fields(cat_id)
        return {
            "category_id":     cat_id,
            "category_l1":     l1,
            "category_l2":     l2,
            "confidence":      round(safe_float(result.get("confidence"), 0.0), 4),
            "method":          "llm_single",
            "requires_review": bool(result.get("requires_review", cat_id == "needs_review.unknown")),
        }
    except Exception as e:
        print(f"  [rag] LLM single failed for '{row.get('concept_norm')}': {e}")
        # Fallback al top-1 aunque no supere el umbral
        cat_id = _validate_category(top1.get("category_id", "needs_review.unknown"))
        l1, l2 = _category_fields(cat_id)
        return {
            "category_id":     cat_id,
            "category_l1":     l1,
            "category_l2":     l2,
            "confidence":      round(top1["score"], 4),
            "method":          "fallback_top1",
            "requires_review": True,
        }


def classify_transactions_batch(
    items: List[dict],
    qdrant: QdrantClient,
) -> Dict[str, dict]:
    """
    Clasifica un batch de transacciones con una sola llamada al LLM.

    Args:
        items: Lista de dicts con keys request_id, row, candidates.
        qdrant: Cliente Qdrant (para fallback individual si el batch falla).

    Returns:
        Dict request_id → resultado de clasificación.
    """
    user_prompt = _build_batch_user_prompt(items)
    try:
        llm_results = ollama_chat_json_array(SYSTEM_PROMPT_BATCH, user_prompt)
    except Exception as e:
        print(f"  [rag] Batch LLM failed: {e}. Fallback to individual.")
        return {
            item["request_id"]: classify_transaction(item["row"], qdrant)
            for item in items
        }

    results = {}
    results = {}
    for raw in llm_results:
        if not isinstance(raw, dict):
            print(f"  [rag] WARN batch item no-dict ignorado: {raw!r}")
            continue

        req_id = raw.get("request_id")
        if not req_id:
            continue

        cat_id = _validate_category(raw.get("category_id", "needs_review.unknown"))
        l1, l2 = _category_fields(cat_id)
        results[req_id] = {
            "category_id":     cat_id,
            "category_l1":     l1,
            "category_l2":     l2,
            "confidence":      round(safe_float(raw.get("confidence"), 0.0), 4),
            "method":          "llm_batch",
            "requires_review": bool(raw.get("requires_review", cat_id == "needs_review.unknown")),
        }

    # Fallback individual para los que el LLM no devolvió
    for item in items:
        if item["request_id"] not in results:
            print(f"  [rag] Missing batch result for '{item['row'].get('concept_norm')}', fallback.")
            results[item["request_id"]] = classify_transaction(item["row"], qdrant)

    return results


def enrich_dataframe(
    df_raw: pd.DataFrame,
    qdrant: QdrantClient,
    iceberg_cache: Optional[Dict[str, dict]] = None,
) -> pd.DataFrame:
    """
    Clasifica todas las transacciones del DataFrame raw.

    Estrategia:
      - Consulta primero la caché Iceberg (concept_norm → category).
      - Para los no cacheados: RAG direct hit o batch LLM.
      - Expande los resultados a todas las filas.
      - Devuelve también new_cache_entries para persistir en Iceberg.

    Args:
        df_raw:         DataFrame raw de transacciones.
        qdrant:         Cliente Qdrant inicializado.
        iceberg_cache:  Dict concept_norm → category cargado desde Iceberg.
                        Si None se trata como caché vacía.

    Returns:
        Tuple (df_enrich, new_cache_entries):
          - df_enrich: DataFrame con columnas category_id, category_l1,
            category_l2, confidence, method, requires_review, enriched_at, batch_id.
          - new_cache_entries: Dict concept_norm → result de los items
            resueltos por LLM en este run (para persistir en Iceberg).
    """
    if df_raw is None or df_raw.empty:
        empty = pd.DataFrame(columns=[
            "txn_id", "category_id", "category_l1", "category_l2",
            "confidence", "method", "requires_review", "enriched_at", "batch_id",
        ])
        return empty, {}

    iceberg_cache = iceberg_cache or {}

    # Pass 1: clasificar tipos únicos
    local_cache: Dict[str, dict] = {}
    new_cache_entries: Dict[str, dict] = {}
    pending: List[dict]          = []

    for _, row in df_raw.iterrows():
        cache_key = str(row.get("concept_norm") or row.get("concept", ""))
        if cache_key in local_cache:
            continue

        # Hard rule → clasificación determinista sin RAG ni LLM
        hard_cat = apply_hard_rules(cache_key)
        if hard_cat:
            l1, l2 = _category_fields(hard_cat)
            local_cache[cache_key] = {
                "category_id":     hard_cat,
                "category_l1":     l1,
                "category_l2":     l2,
                "confidence":      1.0,
                "method":          "hard_rule",
                "requires_review": False,
            }
            continue

        # Hit en caché Iceberg → no llama a Qdrant ni al LLM
        if cache_key in iceberg_cache:
            cached = iceberg_cache[cache_key]
            local_cache[cache_key] = {
                "category_id":     cached.get("category_id",  "needs_review.unknown"),
                "category_l1":     cached.get("category_l1",  "needs_review"),
                "category_l2":     cached.get("category_l2",  "unknown"),
                "confidence":      float(cached.get("confidence") or 0.0),
                "method":          "iceberg_cache",
                "requires_review": cached.get("category_id") == "needs_review.unknown",
            }
            continue

        candidates = retrieve_candidates(row, qdrant)

        # RAG direct hit → resolver inmediatamente sin LLM
        if candidates and candidates[0]["score"] >= RAG_DIRECT_HIT_THRESHOLD:
            top1   = candidates[0]
            cat_id = _validate_category(top1.get("category_id", "needs_review.unknown"))
            l1, l2 = _category_fields(cat_id)
            local_cache[cache_key] = {
                "category_id":     cat_id,
                "category_l1":     l1,
                "category_l2":     l2,
                "confidence":      round(top1["score"], 4),
                "method":          "rag_direct",
                "requires_review": False,
            }
        else:
            pending.append({
                "request_id": f"req_{len(pending)}",
                "cache_key":  cache_key,
                "row":        row,
                "candidates": candidates,
            })

    # Pass 2: resolver pendientes en batches
    if pending:
        # Partir en mini-batches respetando límites de tamaño de prompt
        batches, current_batch = [], []
        for item in pending:
            current_batch.append(item)
            test_prompt = _build_batch_user_prompt(current_batch)
            if len(test_prompt) > MAX_BATCH_USER_PROMPT_LEN or len(current_batch) >= MAX_BATCH_ITEMS:
                if len(current_batch) > 1:
                    batches.append(current_batch[:-1])
                    current_batch = [item]
                else:
                    batches.append(current_batch)
                    current_batch = []
        if current_batch:
            batches.append(current_batch)

        print(f"  [rag] {len(pending)} tipos únicos → {len(batches)} batch(es)")

        for batch in batches:
            batch_results = classify_transactions_batch(batch, qdrant)
            for item in batch:
                result = batch_results[item["request_id"]]
                local_cache[item["cache_key"]] = result
                # Solo persiste en caché los resultados LLM (no RAG direct ni cache hits)
                if result.get("method") not in ("rag_direct", "iceberg_cache"):
                    new_cache_entries[item["cache_key"]] = result

    # Pass 3: expandir a todas las filas
    enriched_at = pd.Timestamp.now("UTC")
    rows = []

    catalog_version = os.getenv("CATALOG_VERSION", "2026-03-20-v1")
    taxonomy_source = "normalization_catalog"
    taxonomy_table = "ALLOWED_CATEGORIES"

    for _, row in df_raw.iterrows():
        cache_key = str(row.get("concept_norm") or row.get("concept", ""))
        result = local_cache.get(cache_key, {
            "category_id":     "needs_review.unknown",
            "category_l1":     "needs_review",
            "category_l2":     "unknown",
            "confidence":      0.0,
            "method":          "fallback_missing",
            "requires_review": True,
        })

        method = result.get("method")
        requires_review = bool(result.get("requires_review", False))

        # Valores derivados de forma segura
        category_id = result.get("category_id", "needs_review.unknown")
        category_l1 = result.get("category_l1", "needs_review")
        category_l2 = result.get("category_l2", "unknown")

        category_path = result.get("category_path")
        if not category_path:
            category_path = category_id

        merchant_norm = result.get("merchant_norm") or row.get("concept_norm")
        canonical_label = result.get("canonical_label") or row.get("concept_norm")

        validation_status = result.get("validation_status")
        if not validation_status:
            validation_status = "pending_review" if requires_review else "auto_accepted"

        # Solo informar de modelo si realmente intervino LLM / retrieval
        model_name = OLLAMA_CHAT_MODEL if method in ("llm_batch", "llm_single") else None
        embed_model_name = OLLAMA_EMBED_MODEL if method in ("rag_direct", "llm_batch", "llm_single") else None
        retrieval_top_k = RAG_TOP_K if method in ("rag_direct", "llm_batch", "llm_single") else None

        # Hash/catálogo/trazabilidad
        catalog_hash = result.get("catalog_hash")
        prompt_hash = result.get("prompt_hash")
        chosen_candidate_id = result.get("chosen_candidate_id")

        candidates_json = result.get("candidates_json")
        if candidates_json is not None and not isinstance(candidates_json, str):
            candidates_json = json.dumps(candidates_json, ensure_ascii=False)

        enrichment_payload = {
            "txn_id": row.get("txn_id"),
            "concept": row.get("concept"),
            "concept_norm": row.get("concept_norm"),
            "merchant_key": row.get("merchant_key"),
            "location": row.get("location"),
            "category_id": category_id,
            "category_l1": category_l1,
            "category_l2": category_l2,
            "confidence": float(result.get("confidence") or 0.0),
            "method": method,
            "requires_review": requires_review,
            "validation_status": validation_status,
            "chosen_candidate_id": chosen_candidate_id,
        }

        rows.append({
            "txn_id":              row.get("txn_id"),
            "merchant_norm":       merchant_norm,
            "canonical_label":     canonical_label,
            "category_l1":         category_l1,
            "category_l2":         category_l2,
            "category_id":         category_id,
            "category_path":       category_path,
            "confidence":          float(result.get("confidence") or 0.0),
            "validation_status":   validation_status,
            "requires_review":     requires_review,
            "catalog_hash":        catalog_hash,
            "taxonomy_source":     taxonomy_source,
            "taxonomy_table":      taxonomy_table,
            "method":              method,
            "model":               model_name,
            "embed_model":         embed_model_name,
            "catalog_version":     catalog_version,
            "prompt_hash":         prompt_hash,
            "retrieval_top_k":     retrieval_top_k,
            "chosen_candidate_id": chosen_candidate_id,
            "candidates_json":     candidates_json,
            "enrichment_json":     json.dumps(enrichment_payload, ensure_ascii=False),
            "enriched_at":         enriched_at,
            "batch_id":            row.get("batch_id"),
        })
        
    return pd.DataFrame(rows), new_cache_entries

def _nullable_int_series(series: pd.Series) -> pd.Series:
    """
    Convierte una serie pandas a enteros nullable compatibles con Spark:
    - NaN / None / pd.NA -> None
    - 3.0 -> 3
    - "5" -> 5
    - cualquier no numérico -> None
    """
    s = pd.to_numeric(series, errors="coerce")
    return pd.Series(
        [int(x) if pd.notna(x) else None for x in s.tolist()],
        index=series.index,
        dtype="object",
    )
    
def normalize_enrichment_for_spark(df: pd.DataFrame) -> pd.DataFrame:
    expected_columns = [f.name for f in ENRICH_SCHEMA.fields]

    if df is None or df.empty:
        return pd.DataFrame(columns=expected_columns)

    df = df.copy()

    for col in expected_columns:
        if col not in df.columns:
            df[col] = None

    text_cols = [
        "txn_id", "merchant_norm", "canonical_label",
        "category_l1", "category_l2", "category_id", "category_path",
        "validation_status", "catalog_hash", "taxonomy_source",
        "taxonomy_table", "method", "model", "embed_model",
        "catalog_version", "prompt_hash", "chosen_candidate_id",
        "candidates_json", "enrichment_json", "batch_id",
    ]
    for col in text_cols:
        if col in df.columns:
            df[col] = df[col].where(df[col].notna(), None)
            df[col] = df[col].apply(lambda x: str(x) if x is not None else None)

    if "confidence" in df.columns:
        df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")

    if "retrieval_top_k" in df.columns:
        df["retrieval_top_k"] = _nullable_int_series(df["retrieval_top_k"])

    def normalize_bool(x):
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return None
        if isinstance(x, bool):
            return x
        if isinstance(x, str):
            v = x.strip().lower()
            if v in {"true", "1", "yes", "y", "si", "sí"}:
                return True
            if v in {"false", "0", "no", "n"}:
                return False
            return None
        if isinstance(x, (int, float)):
            return bool(x)
        return None

    if "requires_review" in df.columns:
        df["requires_review"] = df["requires_review"].apply(normalize_bool).astype("object")

    if "enriched_at" in df.columns:
        s = pd.to_datetime(df["enriched_at"], errors="coerce", utc=True)
        df["enriched_at"] = s.dt.tz_localize(None).astype("datetime64[us]")

    for col in df.columns:
        df[col] = df[col].where(pd.notna(df[col]), None)

    return df[expected_columns]

# ===========================================================================
# ─── CACHÉ RAG SIMPLIFICADA ─────────────────────────────────────────────────
# ===========================================================================
# Persiste las clasificaciones LLM ya computadas para evitar llamadas
# repetidas entre runs. Solo guarda concept_norm → category.
# La caché es best-effort: si falla no aborta el pipeline.

TARGET_TABLE_RAG_CACHE = "lk.silver.finance_txn_rag_cache"

RAG_CACHE_SCHEMA = StructType([
    StructField("concept_norm",  StringType(),    False),
    StructField("category_id",   StringType(),    True),
    StructField("category_l1",   StringType(),    True),
    StructField("category_l2",   StringType(),    True),
    StructField("confidence",    DoubleType(),    True),
    StructField("method",        StringType(),    True),
    #StructField("cached_at",     TimestampNTZType(), True),
    StructField("cached_at",     TimestampType(), True),
])


def load_rag_cache_from_iceberg(spark) -> Dict[str, dict]:
    """
    Carga la caché RAG desde Iceberg en un dict en memoria.
    Devuelve {} si la tabla no existe o está vacía.
    """
    try:
        df = spark.table(TARGET_TABLE_RAG_CACHE).toPandas()
        return {
            row["concept_norm"]: {
                "category_id":  row["category_id"],
                "category_l1":  row["category_l1"],
                "category_l2":  row["category_l2"],
                "confidence":   float(row.get("confidence") or 0.0),
                "method":       row.get("method", "cached"),
            }
            for _, row in df.iterrows()
            if row.get("concept_norm")
        }
    except Exception as e:
        print(f"  [cache] No se pudo cargar caché RAG: {e}")
        return {}


def persist_rag_cache(spark, new_entries: Dict[str, dict]) -> None:
    """
    Persiste nuevas entradas de caché RAG en Iceberg.
    Solo escribe concept_norm que no existían (dedup por concept_norm).
    """
    if not new_entries:
        return
    now  = pd.Timestamp.now("UTC")
    rows = [
        {
            "concept_norm": k,
            "category_id":  v.get("category_id",  "needs_review.unknown"),
            "category_l1":  v.get("category_l1",  "needs_review"),
            "category_l2":  v.get("category_l2",  "unknown"),
            "confidence":   float(v.get("confidence") or 0.0),
            "method":       v.get("method", "unknown"),
            "cached_at":    now,
        }
        for k, v in new_entries.items() if k
    ]
    if not rows:
        return
    try:
        df = pd.DataFrame(rows)
        df["cached_at"] = pd.to_datetime(df["cached_at"]).astype("datetime64[us]")
        sdf = spark.createDataFrame(df, schema=RAG_CACHE_SCHEMA)
        sdf = sdf.dropDuplicates(["concept_norm"])
        sdf.writeTo(TARGET_TABLE_RAG_CACHE).append()
        print(f"  [cache] {len(rows)} entradas escritas en {TARGET_TABLE_RAG_CACHE}")
    except Exception as e:
        print(f"  [cache] WARN – No se pudo persistir caché RAG: {e}")

# ===========================================================================
# ─── ESCRITURA EN ICEBERG ────────────────────────────────────────────────────
# ===========================================================================

def write_to_iceberg(spark_df, target_table: str = TARGET_TABLE, dedupe: bool = True) -> int:
    if spark_df.rdd.isEmpty():
        print("  [iceberg] No rows to write")
        return 0

    if dedupe:
        if target_table == TARGET_TABLE:
            spark_df = spark_df.dropDuplicates(["txn_id"])
        elif target_table == TARGET_TABLE_ENRICH:
            spark_df = spark_df.dropDuplicates(["txn_id"])
        elif target_table == TARGET_TABLE_RAG_CACHE:
            spark_df = spark_df.dropDuplicates(["concept_norm"])

    spark_df.writeTo(target_table).append()
    #spark_df.write.format("iceberg").mode("append").save(target_table)
    return spark_df.count()

# ===========================================================================
# ─── PIPELINE PRINCIPAL ─────────────────────────────────────────────────────
# ===========================================================================

def process_one_pdf_object(
    bucket: str,
    key: str,
    base_password: str,
    s3,
    spark,
    qdrant: QdrantClient,
    write: bool = True,
) -> dict:
    """
    Pipeline completo para un único PDF BBVA.

    Pasos:
      1. Descarga el PDF desde S3/MinIO.
      2. Comprueba marcador SHA-256; salta si ya fue procesado.
      3. Resuelve la contraseña del PDF.
      4. Extrae transacciones (account + card).
      5. Construye el DataFrame Silver raw.
      6. Clasifica cada transacción (RAG + LLM).
      7. Escribe raw y enrichment en Iceberg (si write=True).
      8. Escribe marcador de control en S3.

    Returns:
        Dict con: source_path, ok, skipped, error,
                  rows_parsed, rows_written_raw, rows_written_enrich.
    """
    local_pdf           = None
    logical_source_path = f"s3://{bucket}/{key}"
    file_hash           = None

    try:
        local_pdf = download_pdf_to_tempfile(s3, bucket, key)
        file_hash = sha256_file(local_pdf)

        if was_pdf_already_processed_by_control(s3, bucket, file_hash):
            print(f"  [pipeline] SKIP (already processed): {file_hash}")
            return {
                "source_path": logical_source_path, "ok": True,
                "skipped": True, "skip_reason": "already_processed",
                "error": None, "rows_parsed": 0,
                "rows_written_raw": 0, "rows_written_enrich": 0,
            }

        pdf_password = resolve_pdf_password(local_pdf, base_password)
        df_acc, df_card, _log, full_text = extract_pdf_transactions(
            local_pdf, password=pdf_password
        )

        df_raw = build_silver_dataframe(
            df_acc=df_acc, df_card=df_card,
            pdf_path=local_pdf,
            source_path=logical_source_path,
            raw_text=full_text,
        )
        if df_raw is None:
            df_raw = pd.DataFrame()

        rows_parsed = len(df_raw)
        df_raw      = normalize_silver_for_spark(df_raw)

        df_enrich = enrich_dataframe(df_raw, qdrant)
        df_enrich = normalize_enrichment_for_spark(df_enrich)

        rows_written_raw = rows_written_enrich = 0

        if write and not df_raw.empty:
            spark_raw        = spark.createDataFrame(df_raw, schema=TARGET_SCHEMA)
            rows_written_raw = write_to_iceberg(spark_raw, target_table=TARGET_TABLE)

        if write and not df_enrich.empty:
            spark_enrich          = spark.createDataFrame(df_enrich, schema=ENRICH_SCHEMA)
            rows_written_enrich   = write_to_iceberg(spark_enrich, target_table=TARGET_TABLE_ENRICH)

        # Marcador de control
        control_payload = {
            "source_path":      logical_source_path,
            "source_key":       key,
            "file_hash":        file_hash,
            "bank":             "bbva",
            "status":           "success",
            "rows_parsed":      rows_parsed,
            "rows_written_raw": rows_written_raw,
            "rows_written_enrich": rows_written_enrich,
            "processed_at":     pd.Timestamp.now("UTC").isoformat(),
        }
        put_json_to_s3(s3, bucket=bucket, key=build_processed_control_key(file_hash), payload=control_payload)

        return {
            "source_path":       logical_source_path,
            "ok":                True,
            "skipped":           False,
            "error":             None,
            "rows_parsed":       rows_parsed,
            "rows_written_raw":  rows_written_raw,
            "rows_written_enrich": rows_written_enrich,
        }

    except Exception as e:
        if file_hash:
            try:
                put_json_to_s3(s3, bucket=bucket, key=build_failed_control_key(file_hash), payload={
                    "source_path": logical_source_path, "source_key": key,
                    "file_hash": file_hash, "bank": "bbva", "status": "error",
                    "error": str(e), "processed_at": pd.Timestamp.now("UTC").isoformat(),
                })
            except Exception:
                pass
        return {
            "source_path": logical_source_path, "ok": False,
            "skipped": False, "error": str(e),
            "rows_parsed": 0, "rows_written_raw": 0, "rows_written_enrich": 0,
        }

    finally:
        if local_pdf and os.path.exists(local_pdf):
            os.remove(local_pdf)