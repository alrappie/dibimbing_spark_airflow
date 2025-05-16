import easyocr
import requests
from io import BytesIO
from airflow.hooks.postgres_hook import PostgresHook
import logging
import json
import re

def prompt_llm(input):
    prompt = f"""
    You are a data extraction agent about motorcycle auction.

    Extract the following fields from this motorcycle auction OCR result:
    Rules:
    1. address should be in the format of "Kota, Provinsi" or "Kota, Provinsi, Kode Pos".
    2. price should be below HARGA DASAR and should be a number.
    3. merk_model should be the brand and model of the motorcycle usually found under 'tahun' (takes 2 lines after 'TAHUN').
    4. tahun should be the year of the motorcycle, usually after merk_model.
    5. For CC and odometer:
    - Look for the word 'WARNA'. The first numeric value after 'WARNA' is the CC.
    - If no number is found after 'WARNA', set CC to null.
    - Any numeric value that appears after CC but **before** the section containing 'MESIN' or 'GRADE MESIN' is the odometer.
    - If no such number is found, set odometer to null.
    6. grade_mesin should be the engine condition grade (A to G) found near the engine condition section.
    7. desc_mesin should include all engine-related condition notes exactly as written, up until the 'EKSTERIOR' section, split by ','.
    8. grade_eksterior should be the exterior condition grade (A to G) found near the exterior section.
    9. desc_eksterior should include all exterior-related condition notes exactly as written, split by ','.
    10. Maintain proper JSON formatting.
    11. Don't include any other text, explanation, or notes in the output.
    12. fix the typos in the OCR text.

    Make sure the output is in the following JSON format with no additional code or explanation:
    {{
    "address": "",
    "price": "",
    "merk_model": "",
    "tahun": "",
    "CC": "",
    "odometer": "",
    "grade_mesin": "",
    "desc_mesin": "",
    "grade_eksterior": "",
    "desc_eksterior": ""
    }}

    OCR TEXT:
    \"\"\"{input}\"\"\"
    """

    response = requests.post(
        "http://ollama:11434/api/generate",
        json={
            "model": "qwen2.5:7b-instruct",  # or "llama3:vision" if that's what you're running
            "prompt": prompt,
            "stream": False
        }
    )

    try:
        raw_output = response.json()['response']    
        parsed_output = json.loads(raw_output)  # <-- parse string to dict
    except json.JSONDecodeError:
        logging.error("Failed to parse LLM output as JSON")
        return None
    
    return parsed_output


def process_images():
    reader = easyocr.Reader(['id'])  # English + Indonesian
    failed_list = []
    # Get Postgres connection
    pg_hook = PostgresHook(postgres_conn_id="postgres_dw")
    conn = pg_hook.get_conn()
    cursor = conn.cursor()

    # Create results table if not exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dwd_motor_auction (
            id SERIAL PRIMARY KEY,
            motor_id VARCHAR REFERENCES raw_motor_auctions(motor_id),
            like_count VARCHAR, 
            view_count VARCHAR,
            processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            address TEXT,
            price TEXT,
            merk_model TEXT,
            tahun TEXT,
            CC TEXT,
            odometer TEXT,
            grade_mesin TEXT,
            desc_mesin TEXT,
            grade_eksterior TEXT,
            desc_eksterior TEXT
        )
    """)

    # Fetch all images to process
    cursor.execute("""
        SELECT motor_id, image_url, like_count, view_count
        FROM raw_motor_auctions
    """)

    records = cursor.fetchall()
    for motor_id, image_url, like_count, view_count in records:
        try:
            # Download image
            response = requests.get(
                image_url,
                headers={'User-Agent': 'Mozilla/5.0'},
                timeout=10
            )
            response.raise_for_status()

            # Process with EasyOCR
            image_data = BytesIO(response.content)
            results = reader.readtext(image_data.getvalue())
            extracted_text = [text for (_, text, _) in results]
            # LLM Prompting
            structured_data = prompt_llm(extracted_text)
            print(structured_data)
            if not structured_data:
                logging.warning(f"No structured data returned for motor_id={motor_id}")
                failed_list.append(motor_id)
                continue

            like_count = re.search(r'\d+', like_count).group()
            view_count = re.search(r'\d+', view_count).group()

            # Store results (serialize list to JSON string)
            cursor.execute("""
                INSERT INTO dwd_motor_auction (
                    motor_id, like_count, view_count,
                    address, price, merk_model, tahun, CC, odometer,
                    grade_mesin, desc_mesin, grade_eksterior, desc_eksterior
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                motor_id, like_count, view_count,
                structured_data.get("address"),
                structured_data.get("price"),
                structured_data.get("merk_model"),
                structured_data.get("tahun"),
                structured_data.get("CC"),
                structured_data.get("odometer"),
                structured_data.get("grade_mesin"),
                structured_data.get("desc_mesin"),
                structured_data.get("grade_eksterior"),
                structured_data.get("desc_eksterior")
            ))

            logging.info(f"Processed {motor_id}: {len(extracted_text)} text elements found")
            conn.commit()

        except Exception as e:
            logging.error(f"Failed to process {motor_id}: {str(e)}")
            conn.rollback()

    cursor.close()
    conn.close()

    return failed_list

def retry_failed_images(**kwargs):
    ti = kwargs['ti']
    failed_motor_ids = ti.xcom_pull(task_ids='ocr_llm_transform_load')

    if not failed_motor_ids:
        print("No failed items to retry.")
        return

    reader = easyocr.Reader(['id'])
    pg_hook = PostgresHook(postgres_conn_id="postgres_dw")
    conn = pg_hook.get_conn()
    cursor = conn.cursor()

    for motor_id in failed_motor_ids:
        try:
            cursor.execute("SELECT image_url, like_count, view_count FROM raw_motor_auctions WHERE motor_id = %s", (motor_id,))
            result = cursor.fetchone()
            if not result:
                continue

            image_url, like_count, view_count = result
            response = requests.get(image_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
            response.raise_for_status()

            image_data = BytesIO(response.content)
            results = reader.readtext(image_data.getvalue())
            extracted_text = [text for (_, text, _) in results]
            structured_data = prompt_llm(extracted_text)

            if not structured_data:
                logging.warning(f"Still failed: {motor_id}")
                continue

            like_count = re.search(r'\d+', like_count).group()
            view_count = re.search(r'\d+', view_count).group()

            cursor.execute("""
                INSERT INTO dwd_motor_auction (
                    motor_id, like_count, view_count,
                    address, price, merk_model, tahun, CC, odometer,
                    grade_mesin, desc_mesin, grade_eksterior, desc_eksterior
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                motor_id, like_count, view_count,
                structured_data.get("address"),
                structured_data.get("price"),
                structured_data.get("merk_model"),
                structured_data.get("tahun"),
                structured_data.get("CC"),
                structured_data.get("odometer"),
                structured_data.get("grade_mesin"),
                structured_data.get("desc_mesin"),
                structured_data.get("grade_eksterior"),
                structured_data.get("desc_eksterior")
            ))

            conn.commit()
            logging.info(f"Retry success for {motor_id}")

        except Exception as e:
            logging.error(f"Retry failed for {motor_id}: {e}")
            conn.rollback()

    cursor.close()
    conn.close()