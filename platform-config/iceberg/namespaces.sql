-- Namespace model for local MVP
-- Execute using Spark SQL or equivalent catalog client after Polaris catalog exists.
CREATE NAMESPACE IF NOT EXISTS lakehouse.bronze;
CREATE NAMESPACE IF NOT EXISTS lakehouse.silver;
CREATE NAMESPACE IF NOT EXISTS lakehouse.gold;
CREATE NAMESPACE IF NOT EXISTS lakehouse.demo;
