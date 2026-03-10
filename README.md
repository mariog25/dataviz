# 1) Stack Lakehouse/Gobierno de Datos local (Docker Compose)

Guía técnica y operativa.

> **Objetivo**: entorno local tipo lakehouse para desarrollo y pruebas con MinIO, Spark, Iceberg, catálogo REST (Polaris), Trino, DataHub, Jupyter y Airflow opcional.
>
> **Nota importante**: en el estado actual versionado del repo, el catálogo activo en `docker-compose` es **Polaris** (no Nessie) y **Vault no está declarado** como servicio de Compose. En este README se incluyen pasos de Vault y validaciones de Nessie como **plantilla operativa** para entornos que lo requieran.

---

# 2) Arquitectura funcional del stack

| Componente | Rol | Estado en este repo |
|---|---|---|
| MinIO | Object storage S3-compatible (landing/silver/gold) | Activo en Compose |
| Spark | ETL / escritura Iceberg | Activo en Compose |
| Iceberg | Capa Lakehouse | Activo vía Spark/Trino |
| Nessie | Catálogo rest | Activo en Compose |
| Trino | SQL endpoint | Activo en Compose |
| DataHub | Catálogo-linkage, metadata, lineage, Gobierno del Dato | Activo en Compose |
| Airflow | Orquestación ETLs - DAGs | Activo en Compose |
| JupyterLab | Notebooks de desarrollo | Activo en Compose |
| Vault | Secret management | Activo en Compose |


Estructura lógica de datos recomendada en object storage:
- `landing/`
- `silver/`
- `gold/`

Ejemplo de dataset:
- `landing/financial/bbva/`
- `landing/financial/bbva/manifests/`

---

# 3) Prerrequisitos de equipo

## 3.1 Software
- Docker Engine + Docker Compose v2.
- Linux o WSL2 Ubuntu en Windows 11.
- Bash, curl, sed/perl.

## 3.2 Recursos mínimos
- **Mínimo**: 16 GB RAM.
- **Recomendado**: 24–32 GB RAM.
- CPU: 6 vCPU recomendadas.

## 3.3 Puertos usados

| Servicio | Puerto host |
|---|---:|
| MinIO API | 9000 |
| MinIO Console | 9001 |
| Trino | 8080 |
| Polaris API | 8181 |
| JupyterLab | 8888 |
| DataHub Frontend | 9002 |
| DataHub GMS | 8084 |
| Airflow (opcional) | 8081 |

Comprobación rápida de puertos ocupados:
```bash
ss -ltnp | egrep ':9000|:9001|:8080|:8181|:8888|:9002|:8084|:8081' || true
```
**Validación**: si aparece un proceso ajeno, libera ese puerto antes de arrancar.

---

# 4) Estructura del proyecto

```bash
.
├── platform-infra/
│   ├── docker-compose.yml
│   ├── .env.example
│   ├── init-scripts/
│   │   ├── 00_validate_env.sh
│   │   ├── 01_minio_create_buckets.sh
│   │   ├── 02_bootstrap_polaris.sh
│   │   └── 03_smoke_test.sh
│   ├── configs/
│   └── docker/
├── platform-config/
└── data-workloads/
```

---

# 5) Configuración inicial desde cero (solo primera instalación)

## 5.1 Clonar y preparar `.env`
```bash
git clone <TU_REPO_URL> dataviz
cd dataviz/platform-infra
cp .env.example .env
```
**Validación**: `test -f .env && echo OK`

## 5.2 Ajustar variables sensibles en `.env`
Edita al menos:
- `MINIO_ROOT_USER=<MINIO_ACCESS_KEY>`
- `MINIO_ROOT_PASSWORD=<MINIO_SECRET_KEY>`
- `POLARIS_BOOTSTRAP_CREDENTIALS=root=<DATAHUB_SECRET>` (formato `usuario=password`)
- `JUPYTER_TOKEN=<DATAHUB_SECRET>`

```bash
sed -n '1,120p' .env
```
**Validación**: confirma que no hay comillas rotas ni caracteres CRLF.

## 5.3 Crear directorios persistentes en host (recomendado)

Aunque el `compose` actual usa volúmenes nombrados, para backup/migración es recomendable bind mount local.

```bash
sudo mkdir -p /home/datalakehouse/{minio,polaris-db,datahub-mysql,datahub-es,vault}
sudo chown -R $USER:$USER /home/datalakehouse
```
**Validación**:
```bash
ls -la /home/datalakehouse
```

## 5.4 (Opcional recomendado) override local de volúmenes en host
Crea archivo **plantilla local** `docker-compose.override.yml` (no versionado):

```bash
cat > docker-compose.override.yml <<'YAML'
services:
  minio:
    volumes:
      - /home/datalakehouse/minio:/data
  polaris-db:
    volumes:
      - /home/datalakehouse/polaris-db:/var/lib/postgresql/data
  datahub-mysql:
    volumes:
      - /home/datalakehouse/datahub-mysql:/var/lib/mysql
  datahub-elasticsearch:
    volumes:
      - /home/datalakehouse/datahub-es:/usr/share/elasticsearch/data
YAML
```
**Validación**:
```bash
docker compose config >/tmp/compose.rendered.yaml && echo "compose ok"
```

---

# 6) Arranque e inicialización de Vault (separado, solo primera instalación)

> En este repo Vault no viene definido en `docker-compose.yml`. Levántalo aparte o en otro compose.

## 6.1 Arrancar Vault dev (local laboratorio)
```bash
docker run -d --name vault-dev \
  --cap-add=IPC_LOCK \
  -p 8200:8200 \
  -e VAULT_DEV_ROOT_TOKEN_ID=<VAULT_TOKEN> \
  -e VAULT_DEV_LISTEN_ADDRESS=0.0.0.0:8200 \
  -v /home/datalakehouse/vault:/vault/file \
  hashicorp/vault:1.17
```
**Validación**:
```bash
curl -s http://localhost:8200/v1/sys/health
```

## 6.2 Inicializar / unseal (si usas modo no-dev)
```bash
export VAULT_ADDR=http://localhost:8200
vault operator init
vault operator unseal <UNSEAL_KEY_1>
vault operator unseal <UNSEAL_KEY_2>
vault operator unseal <UNSEAL_KEY_3>
```
**Validación**: `vault status` debe mostrar `Sealed false`.

## 6.3 Autenticarse y guardar secreto BBVA
```bash
export VAULT_ADDR=http://localhost:8200
export VAULT_TOKEN=<VAULT_TOKEN>

vault kv put secret/pipelines/bbva \
  datahub_pat=<DATAHUB_PAT> \
  minio_access_key=<MINIO_ACCESS_KEY> \
  minio_secret_key=<MINIO_SECRET_KEY>
```
**Validación**:
```bash
vault kv get secret/pipelines/bbva
```

---

# 7) Arranque controlado de DataHub (separado por fases)

## 7.1 Dependencias previas
- Docker operativo.
- Variables en `.env` coherentes.
- Puertos 9002 y 8084 libres.

## 7.2 Orden de arranque de DataHub
```bash
cd /ruta/a/dataviz/platform-infra

docker compose up -d datahub-zookeeper datahub-kafka datahub-mysql datahub-elasticsearch

docker compose up -d datahub-gms

docker compose up -d datahub-frontend
```
**Validación**:
```bash
docker compose ps | egrep 'datahub-(zookeeper|kafka|mysql|elasticsearch|gms|frontend)'
```

## 7.3 Esperar disponibilidad GMS y frontend
```bash
until curl -fsS http://localhost:8084/config >/dev/null; do sleep 3; done
until curl -fsS http://localhost:9002 >/dev/null; do sleep 3; done
echo "DataHub listo"
```
**Validación**: ambos endpoints responden 200/30x.

## 7.4 Generar PAT de DataHub
Método recomendado (UI):
1. Abrir `http://localhost:9002`.
2. Iniciar sesión con credenciales configuradas en tu instancia.
3. Perfil de usuario → Access Tokens → Create Token.
4. Guardar token como `<DATAHUB_PAT>`.

## 7.5 Validar PAT
```bash
curl -i -H "Authorization: Bearer <DATAHUB_PAT>" http://localhost:8084/config
```
**Validación**: no debe responder `401`.

---

# 8) Actualización de entorno antes del arranque final

## 8.1 Variables clave en `.env`
Asegúrate de revisar:
- `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`
- `POLARIS_BOOTSTRAP_CREDENTIALS`
- `POLARIS_URI`, `POLARIS_REST_URI`, `POLARIS_CATALOG_NAME`, `POLARIS_NAMESPACE`
- `JUPYTER_TOKEN`

```bash
grep -E 'MINIO_|POLARIS_|JUPYTER_TOKEN' .env
```
**Validación**: formato correcto y sin valores vacíos.

## 8.2 Integración con secretos de Vault
Si tu pipeline lee secretos desde Vault, copia/inyecta:
- `<DATAHUB_PAT>`
- `<MINIO_ACCESS_KEY>`
- `<MINIO_SECRET_KEY>`
- `<DATAHUB_SECRET>`

**Validación**: prueba lectura de secretos con `vault kv get`.

---

# 9) Arranque del stack completo

```bash
cd /ruta/a/dataviz/platform-infra

# Preflight de entorno
bash ./init-scripts/00_validate_env.sh

# Levantar infraestructura base
docker compose up -d --build

# Buckets MinIO
bash ./init-scripts/01_minio_create_buckets.sh

# Bootstrap catálogo/namespaces Polaris
POLARIS_URI=http://localhost:8181 bash ./init-scripts/02_bootstrap_polaris.sh

# Crear tabla demo con Spark
docker compose exec spark spark-submit /opt/data-workloads/spark_jobs/write_demo_table.py

# Smoke test global
bash ./init-scripts/03_smoke_test.sh
```
**Validación**: el último script debe terminar en `SUCCESS`.

---

# 10) Verificaciones funcionales post-arranque

## 10.1 MinIO
```bash
curl -f http://localhost:9000/minio/health/live
```
**Validación**: HTTP 200.

## 10.2 Trino
```bash
curl -f http://localhost:8080/v1/info
```
**Validación**: JSON con versión.

## 10.3 Airflow (si perfil activo)
```bash
curl -I http://localhost:8081
```
**Validación**: responde HTTP.

## 10.4 DataHub
```bash
curl -f http://localhost:8084/config
curl -I http://localhost:9002
```
**Validación**: GMS + frontend activos.

## 10.5 Nessie (plantilla)
> No está en el compose actual. Si lo habilitas en tu variante:
```bash
curl -f http://localhost:19120/api/v2/config
```
**Validación**: respuesta JSON de configuración Nessie.

## 10.6 Spark
```bash
docker compose exec spark spark-sql -e "SHOW NAMESPACES IN lakehouse"
```
**Validación**: lista namespaces (incluyendo `demo`).

---

# 11) Pruebas iniciales operativas

## 11.1 Contenedores arriba
```bash
docker compose ps
```
**Validación**: estado `Up` para servicios principales.

## 11.2 Buckets/directorios de datos
```bash
docker compose run --rm --profile init minio-init sh -lc 'mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc ls local'
```
**Validación**: existe bucket `lakehouse` (y otros definidos).

## 11.3 Conexión Trino e Iceberg
```bash
docker compose exec trino trino --server http://localhost:8080 --execute "SHOW SCHEMAS FROM iceberg"
docker compose exec trino trino --server http://localhost:8080 --execute "SELECT * FROM iceberg.demo.sample_orders"
```
**Validación**: devuelve schemas y filas de ejemplo.

## 11.4 Topics Kafka de DataHub
```bash
docker compose exec datahub-kafka kafka-topics --bootstrap-server datahub-kafka:9092 --list | sort
```
**Validación**: aparecen topics de metadata/auditoría de DataHub.

## 11.5 Consumer groups y mae-consumer
```bash
docker compose exec datahub-kafka kafka-consumer-groups --bootstrap-server datahub-kafka:9092 --list
```
**Validación**: identificar grupos relacionados con DataHub/MAE.

Si tu despliegue incluye `mae-consumer` como servicio separado:
```bash
docker compose logs --tail=200 mae-consumer
```
**Validación**: sin errores de deserialización/conexión y con consumo activo.

---

# 12) Troubleshooting (errores probables)

## 12.1 Puertos ocupados
Síntoma: `bind: address already in use`.
```bash
ss -ltnp | egrep ':9000|:9001|:8080|:8181|:8888|:9002|:8084|:8081'
```
Solución: parar proceso conflictivo o cambiar mapeo de puertos.

## 12.2 Permisos en volúmenes host
Síntoma: errores de escritura en MinIO/MySQL/ES.
```bash
sudo chown -R $USER:$USER /home/datalakehouse
```

## 12.3 Elasticsearch no healthy
```bash
docker compose logs --tail=200 datahub-elasticsearch
curl -s http://localhost:9200/_cluster/health?pretty
```
Esperado: `status: yellow/green`.

## 12.4 DataHub sin lineage visible
- Verifica ingestores/emitters.
- Revisa GMS logs.
- Si aplica, ejecutar restauración de índices:
```bash
# plantilla: depende de tu distribución de DataHub
# docker compose exec datahub-gms <restore-indices-command>
```
> Marca este paso como repetible tras cambios masivos de metadata/lineage.

## 12.5 Airflow sin conexión `datahub_rest_default`
- Revisa conexión en UI de Airflow (Admin > Connections).
- Valida host/puerto/token de DataHub.
- Reintenta DAG tras corregir conexión.

## 12.6 Problemas con `mae-consumer`
- Revisar lag/estado de consumer groups.
- Confirmar topics existentes y accesibles.
- Confirmar conectividad a Kafka desde el contenedor del consumer.

## 12.7 `02_bootstrap_polaris.sh` falla por URI o credenciales
```bash
printf '%q\n' "$POLARIS_URI"
bash ./init-scripts/00_validate_env.sh
docker compose logs --tail=100 polaris
```
- `POLARIS_BOOTSTRAP_CREDENTIALS` debe ser `usuario=password`.
- Usa `POLARIS_URI=http://localhost:8181` al lanzar el script desde host.

---

# 13) Secuencia recomendada de arranque desde cero

1. **(Solo primera instalación)** crear directorios persistentes host.
2. Preparar `.env` desde `.env.example` y completar secretos.
3. Arrancar e inicializar **Vault** (si aplica en tu entorno).
4. Arrancar dependencias de **DataHub** por fases y validar GMS/frontend.
5. Generar **PAT de DataHub** y guardarlo en Vault/entorno.
6. Levantar stack completo (`docker compose up -d --build`).
7. Ejecutar `00_validate_env.sh`.
8. Ejecutar `01_minio_create_buckets.sh`.
9. Ejecutar `02_bootstrap_polaris.sh`.
10. Ejecutar writer Spark demo.
11. Ejecutar `03_smoke_test.sh`.
12. Validar Trino, DataHub, Jupyter, MinIO, Airflow.

---

# 14) Reinicio posterior del stack ya inicializado

Para reinicios normales (sin reprovisionar estado):
```bash
cd /ruta/a/dataviz/platform-infra
docker compose stop
docker compose start
```
**Validación**: `docker compose ps` en `Up`.

Si necesitas reconstruir imágenes sin borrar estado:
```bash
docker compose up -d --build
```

> Evita `docker compose down -v` si no quieres perder volúmenes.

---

# 15) Orden de apagado recomendado

1. Detener jobs/DAGs activos (Airflow/Jupyter).
2. Parar servicios de consulta/ingesta (Trino, Spark jobs, DataHub ingest).
3. `docker compose stop`.
4. Si necesitas apagar totalmente: `docker compose down` (**sin `-v`**).

```bash
cd /ruta/a/dataviz/platform-infra
docker compose stop
# opcional
docker compose down
```

---

# 16) Checklist final de validación

- [ ] `.env` existe y secretos no están vacíos.
- [ ] `00_validate_env.sh` pasa.
- [ ] MinIO responde healthcheck.
- [ ] Polaris responde en `:8181`.
- [ ] Trino responde en `:8080` y consulta `iceberg.demo.sample_orders`.
- [ ] DataHub GMS y Frontend disponibles.
- [ ] Jupyter accesible en `:8888` con token válido.
- [ ] (Opcional) Airflow accesible en `:8081`.
- [ ] Buckets creados (`lakehouse`, etc.).
- [ ] Volúmenes persistentes confirmados (named volumes o bind mounts host).
