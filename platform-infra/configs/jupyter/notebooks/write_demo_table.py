from pyspark.sql import SparkSession

spark = (
    SparkSession.builder.appName("jupyter-iceberg-demo")
    .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
    .config("spark.sql.catalog.lakehouse", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.lakehouse.type", "rest")
    .config("spark.sql.catalog.lakehouse.uri", "http://polaris:8181/api/catalog")
    .config("spark.sql.catalog.lakehouse.warehouse", "s3a://lakehouse/warehouse")
    .config("spark.hadoop.fs.s3a.endpoint", "http://minio:9000")
    .config("spark.hadoop.fs.s3a.access.key", "minioadmin")
    .config("spark.hadoop.fs.s3a.secret.key", "minioadmin123")
    .config("spark.hadoop.fs.s3a.path.style.access", "true")
    .config("spark.hadoop.fs.s3a.connection.ssl.enabled", "false")
    .getOrCreate()
)

spark.sql("CREATE NAMESPACE IF NOT EXISTS lakehouse.demo")
df = spark.createDataFrame([(1, "widget", 5), (2, "cable", 9)], ["id", "item", "qty"])
df.writeTo("lakehouse.demo.sample_orders").using("iceberg").createOrReplace()
spark.sql("SELECT * FROM lakehouse.demo.sample_orders").show()
