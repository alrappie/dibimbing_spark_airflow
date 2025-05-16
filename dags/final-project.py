from datetime import timedelta, datetime
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.hooks.postgres_hook import PostgresHook
import logging
from utils.scraping_load import scrape_and_load
from utils.ocr_llm_load import process_images, retry_failed_images
from airflow.operators.python import ShortCircuitOperator

default_args = {
    "owner": "alrappie",
    "retries": 0,
    "retry_delay": timedelta(minutes=1),
}


def print_hello():
    logging.getLogger("airflow.task").info("👋 Airflow is working correctly!")
    return "Success!"

def check_if_retry_needed(**kwargs):
    ti = kwargs['ti']
    failed = ti.xcom_pull(task_ids='ocr_llm_transform_load')
    return bool(failed)

with DAG(
    dag_id="final_project_dag",
    default_args=default_args,
    schedule_interval=None,
    start_date=datetime(2024, 1, 1),
    catchup=False,
    dagrun_timeout=timedelta(minutes=60),
    tags=["scraping", "postgres", "example"],
    description="Scrape links and save to Postgres with PostgresHook",
    params={
        "page": 2,
    }
) as dag:

    hello_task = PythonOperator(
        task_id='print_test_message',
        python_callable=print_hello
    )

    scrape_task = PythonOperator(
        task_id='scrape_and_load',
        python_callable=scrape_and_load
    )

    ocr_task = PythonOperator(
        task_id='ocr_llm_transform_load',
        python_callable=process_images
    )

    check_retry_needed = ShortCircuitOperator(
    task_id='check_if_retry_needed',
    python_callable=check_if_retry_needed
    )
    
    retry_task = PythonOperator(
    task_id='retry_failed_ocr',
    python_callable=retry_failed_images,
    provide_context=True,  # optional in Airflow 2.x but safe to add
    )


    # hello_task >> scrape_task >> ocr_task
    hello_task >> scrape_task >> ocr_task >> check_retry_needed >> retry_task