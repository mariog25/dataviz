FROM bitnami/spark:3.5.6

USER root
RUN install_packages curl && \
    mkdir -p /opt/bitnami/spark/jars-extra && \
    curl -fL -o /opt/bitnami/spark/jars-extra/iceberg-spark-runtime-3.5_2.12-1.6.1.jar \
      https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-3.5_2.12/1.6.1/iceberg-spark-runtime-3.5_2.12-1.6.1.jar && \
    curl -fL -o /opt/bitnami/spark/jars-extra/hadoop-aws-3.3.4.jar \
      https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/3.3.4/hadoop-aws-3.3.4.jar && \
    curl -fL -o /opt/bitnami/spark/jars-extra/aws-java-sdk-bundle-1.12.262.jar \
      https://repo1.maven.org/maven2/com/amazonaws/aws-java-sdk-bundle/1.12.262/aws-java-sdk-bundle-1.12.262.jar

ENV SPARK_EXTRA_CLASSPATH=/opt/bitnami/spark/jars-extra/*
USER 1001
