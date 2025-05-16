from datetime import timedelta, datetime
from airflow.hooks.postgres_hook import PostgresHook
from bs4 import BeautifulSoup
import requests
import logging
import re
from psycopg2.extras import execute_batch

def scrape_and_load(**kwargs):
    input_page = kwargs['dag_run'].conf.get('page', 2)
    logger = logging.getLogger("airflow.task")
    base_url = 'https://www.jba.co.id'  # 🔁 Change this to your target site
    headers = {'User-Agent': 'Mozilla/5.0'}
    scraped_data = {}
    pattern = re.compile(r'^.*-1\.(jpg|jpeg|png)$', re.IGNORECASE)

    try:
        for page_num in range(1, input_page):
            url = base_url + '/id/lelang-motor/search?keyword=&vehicle_type=bike&page='+ str(page_num)
            print(f"\nScraping page {page_num} - {url}")
            
            response = requests.get(url, headers=headers)
            if response.status_code != 200:
                print(f"Failed to fetch page {page_num}")
                continue

            soup = BeautifulSoup(response.text, 'html.parser')

            product_list = soup.find('div', class_='product-list')
            if not product_list:
                print("No product list found on this page.")
                continue

            rows = product_list.find('div', class_='row')
            if not rows:
                print("No rows found in product list.")
                continue

            # print(rows.find('div', class_='product-item').find('a').get('href'))
            
            for item in rows.find_all('div', class_='product-item'):
                a_tag = item.find('a')
                if a_tag and a_tag.get('href'):
                    view_data = item.find('div',class_='view-count').text.strip()
                    like_data = item.find('div',class_='like-count').text.strip()
                    motor_id = a_tag.get('href').split('/')[-1]

                    url_motor = base_url + a_tag.get('href')
                    # url_motor = base_url+'/id/lelang-motor/search-detail/4117/640401'
                    response = requests.get(url_motor, headers=headers)
                    if response.status_code != 200:
                        # print(f"Failed to fetch {url_motor}")
                        continue
                    
                    soup = BeautifulSoup(response.text, 'html.parser')
                    for img_tag in soup.find_all('img'):
                        img_src = img_tag.get('src')
                        if img_src and pattern.search(img_src):
                            scraped_data[motor_id] = {
                            'motor_id': motor_id,
                            'like_count': like_data,
                            'view_count': view_data,
                            'motor_url': url_motor,
                            'image_url': img_src,
                            'scraped_at': datetime.now().isoformat()
                        }

        # Use Airflow's Postgres connection
        pg_hook = PostgresHook(postgres_conn_id="postgres_dw")

        # Create table if not exists
        pg_hook.run("""
            CREATE TABLE IF NOT EXISTS raw_motor_auctions (
                id SERIAL PRIMARY KEY,
                motor_id VARCHAR UNIQUE NOT NULL,
                like_count VARCHAR,
                view_count VARCHAR,
                motor_url TEXT,
                image_url TEXT,
                scraped_at TIMESTAMP
            );
        """)

        # Prepare data for bulk insert
        if scraped_data:
            columns = ['motor_id', 'like_count', 'view_count', 'motor_url', 'image_url', 'scraped_at']
            rows = [tuple(data[col] for col in columns) for data in scraped_data.values()]
            
            # Use execute_batch for efficient bulk insert
            conn = pg_hook.get_conn()
            cursor = conn.cursor()
            
            insert_sql = """
                INSERT INTO raw_motor_auctions (motor_id, like_count, view_count, motor_url, image_url, scraped_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (motor_id) DO UPDATE SET
                    like_count = EXCLUDED.like_count,
                    view_count = EXCLUDED.view_count,
                    motor_url = EXCLUDED.motor_url,
                    image_url = EXCLUDED.image_url,
                    scraped_at = EXCLUDED.scraped_at;
            """
            
            execute_batch(cursor, insert_sql, rows)
            conn.commit()
            logger.info(f"✅ Successfully inserted/updated {len(scraped_data)} records")
        else:
            logger.warning("⚠️ No data scraped to insert")

    except Exception as e:
        logger.error(f"❌ Error during scraping or saving: {e}")
        raise