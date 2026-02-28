import os
from pyspark.sql import SparkSession


def build_spark() -> SparkSession:
    minio_user = os.getenv("MINIO_ROOT_USER", "minioadmin")
    minio_pass = os.getenv("MINIO_ROOT_PASSWORD", "minioadmin123")
    polaris_uri = os.getenv("POLARIS_URI", "http://polaris:8181/api/catalog")
    warehouse = os.getenv("ICEBERG_WAREHOUSE", "s3a://lakehouse/warehouse")

    return (
        SparkSession.builder.appName("write-demo-iceberg-table")
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
        .config("spark.sql.catalog.lakehouse", "org.apache.iceberg.spark.SparkCatalog")
        .config("spark.sql.catalog.lakehouse.type", "rest")
        .config("spark.sql.catalog.lakehouse.uri", polaris_uri)
        .config("spark.sql.catalog.lakehouse.warehouse", warehouse)
        .config("spark.sql.defaultCatalog", "lakehouse")
        .config("spark.hadoop.fs.s3a.endpoint", "http://minio:9000")
        .config("spark.hadoop.fs.s3a.access.key", minio_user)
        .config("spark.hadoop.fs.s3a.secret.key", minio_pass)
        .config("spark.hadoop.fs.s3a.path.style.access", "true")
        .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "false")
        .getOrCreate()
    )


def main() -> None:
    spark = build_spark()

    spark.sql("CREATE NAMESPACE IF NOT EXISTS lakehouse.demo")

    rows = [
        (1, "alice", 120.50),
        (2, "bob", 75.00),
        (3, "carol", 300.25),
    ]

    df = spark.createDataFrame(rows, ["order_id", "customer", "amount"])
    (
        df.writeTo("lakehouse.demo.sample_orders")
        .using("iceberg")
        .tableProperty("format-version", "2")
        .createOrReplace()
    )

    spark.sql("SELECT * FROM lakehouse.demo.sample_orders ORDER BY order_id").show()
    spark.stop()


if __name__ == "__main__":
    main()
