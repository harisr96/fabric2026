# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "70497f1b-3e54-b5af-4452-5d47d37a55ef",
# META       "default_lakehouse_name": "silver",
# META       "default_lakehouse_workspace_id": "00000000-0000-0000-0000-000000000000",
# META       "known_lakehouses": [
# META         {
# META           "id": "d1cceb0f-d455-86a6-4050-654d38261cb7",
# META           "workspace_id": "00000000-0000-0000-0000-000000000000"
# META         },
# META         {
# META           "id": "70497f1b-3e54-b5af-4452-5d47d37a55ef",
# META           "workspace_id": "00000000-0000-0000-0000-000000000000"
# META         }
# META       ]
# META     },
# META     "warehouse": {
# META       "known_warehouses": []
# META     }
# META   }
# META }

# CELL ********************


# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

 
workspace = notebookutils.runtime.context["currentWorkspaceName"]
is_prod = workspace.upper().endswith("-PROD")
print(f"Running in {workspace} (PROD: {is_prod})")

spark.sql("""
CREATE TABLE IF NOT EXISTS bronze.dbo.customers (
    customer_id INT, first_name STRING, last_name STRING, country STRING
) USING DELTA
""")
spark.sql("""
CREATE TABLE IF NOT EXISTS bronze.dbo.orders (
    order_id INT, customer_id INT, order_date DATE, amount DECIMAL(10,2)
) USING DELTA
""")

if not is_prod and spark.sql("SELECT COUNT(*) AS n FROM bronze.dbo.customers").first()["n"] == 0:
    spark.sql("""INSERT INTO bronze.dbo.customers VALUES
        (1, 'Asha', 'Rao', 'AU'), (2, 'Ben', 'Lee', 'NZ'), (3, 'Chloe', 'Ng', 'AU')""")

if not is_prod and spark.sql("SELECT COUNT(*) AS n FROM bronze.dbo.orders").first()["n"] == 0:
    spark.sql("""INSERT INTO bronze.dbo.orders VALUES
        (101, 1, DATE'2026-10-01', 120.50), (102, 1, DATE'2026-10-15', 80.00),
        (103, 2, DATE'2026-10-20', 45.25),  (104, 3, DATE'2026-10-28', 300.00)""")


# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

silver_df = spark.sql("""
SELECT c.customer_id, c.first_name, c.last_name, c.country,
       COUNT(o.order_id)            AS order_count,
       COALESCE(SUM(o.amount), 0)   AS total_amount
FROM bronze.dbo.customers AS c
LEFT JOIN bronze.dbo.orders AS o ON o.customer_id = c.customer_id
GROUP BY c.customer_id, c.first_name, c.last_name, c.country
""")

(silver_df.write.mode("overwrite")
    .option("overwriteSchema", "true")
    .saveAsTable("silver.dbo.customer_summary"))

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

display(spark.sql("SELECT * FROM bronze.dbo.customers"))
display(spark.sql("SELECT * FROM silver.dbo.customer_summary ORDER BY total_amount DESC"))

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************


# CELL ********************

display(spark.sql("SELECT * FROM bronze.dbo.orders"))

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

spark.sql("""INSERT INTO bronze.dbo.orders VALUES
        (105, 1, DATE'2026-10-01', 120.50), (102, 1, DATE'2026-10-15', 80.00),
        (106, 2, DATE'2026-10-20', 45.25),  (104, 3, DATE'2026-10-28', 300.00)""")


# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

display(spark.sql("SELECT * FROM bronze.dbo.orders"))

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
