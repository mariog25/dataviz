from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime

def say_hello():
    print("Hola desde tu lakehouse local 🚀")

with DAG(
    dag_id="hello_lakehouse",
    start_date=datetime(2024, 1, 1),
    schedule=None,  
    catchup=False,
    tags=["demo"],
) as dag:

    hello_task = PythonOperator(
        task_id="say_hello_task",
        python_callable=say_hello,
    )
