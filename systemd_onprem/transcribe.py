import os
from dotenv import load_dotenv
load_dotenv()  # take environment variables from .env.
import json
import urllib3
import urllib.parse
import boto3
import mysql.connector
from mysql.connector import Error
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, as_completed
import warnings
from urllib3.exceptions import InsecureRequestWarning
# Import Semaphore and add it as a limit manager
from threading import Semaphore, BoundedSemaphore
import time

# Semaphore size corresponds to the connection pool size
sem = Semaphore(5)
transcribing_sema = BoundedSemaphore(5)


warnings.filterwarnings("ignore", category=InsecureRequestWarning)

# Create client for interacting with s3 bucket
s3 = boto3.client('s3')
ssm = boto3.client('ssm')

http = urllib3.PoolManager(
    cert_reqs='CERT_NONE'
)


db_config = {
    "host":'mysql00.c3qe6y4e4dwv.eu-west-2.rds.amazonaws.com',
    'user': 'eicsqa_admin',
    'passwd': os.getenv('DBPWD'),
    'database':'CCIQ'
}

def get_database_connection():
    # Create a new connection for each thread
    try:
        con = mysql.connector.connect(**db_config)
    except Error as e:
        print(f"Error while connecting to MySQL: {e}", flush=True)
        con = None
    return con


def get_object_size(bucket_name, file_key):
    try:
        response = s3.head_object(Bucket=bucket_name, Key=file_key)
        return response["ContentLength"]
    except Exception as e:
        print(f"Error while retrieving object size: {e}")
        return None



def execute_query(query, *args):
    with closing(get_database_connection()) as con:
        with closing(con.cursor()) as cur:
            cur.execute(query, *args)
            return cur.fetchall()

def get_s3_presigned_url(bucket_name, object_name, expiration=300):
    "Generate a presigned URL to share an S3 object"
    try:
        response = s3.generate_presigned_url('get_object',
                                             Params={'Bucket': bucket_name,
                                                     'Key': object_name},
                                             ExpiresIn=expiration)
    except Exception as e:
        print(f"Error generating presigned URL: {e}")
        return None
    return response

def llm_transcribe(bucket_name: str, file_key: str, lang: str):
    "Transcribe using transcribers.gamesys.corp VIP on Netscaler"
    # Generate presigned URL for the audio file
    presigned_url = get_s3_presigned_url(bucket_name, file_key)
    if not presigned_url:
        raise Exception("Failed to generate presigned URL")
    res = http.request(
        method="POST",
        url="https://transcribers.gamesys.corp/predictions",
        headers={
            "accept": "application/json",
            "Content-Type": "application/json",
        },
        body=json.dumps(
            {
                "input": {
                    "audio": presigned_url,
                    "task": "transcribe",
                    "language": lang,
                    "batch_size": 8, ### Batch size is set according to the 16GB VRAM available for Nvidia T4 and P100. Increase for more capable cards.
                    "timestamp": "chunk",
                    "diarise_audio": "false",
                    "hf_token": ""
                }
            }
        )
    )
    if res.status == 200:
        response = json.loads(res.data.decode('utf-8'))
        if response['status'] == 'succeeded':
            return(response['output'])
        else:
            raise Exception(f"Error: {res.status}, {res.data.decode('utf-8')}")
    else:
        raise Exception(f"Error: {res.status}, {res.data.decode('utf-8')}")

def handle_database_operation(operation, conversation_id=None, ticket_id=None, url=None, lang=None, pii=None):
    with sem:  # Acquire semaphore
        con = get_database_connection()
        if con is not None:
            try:
                cur = con.cursor()
                if operation == "insert":
                    sql_query = """
                        INSERT INTO FILES (ticket_id, pii, type, url, lang)
                        VALUES (%s, %s, %s, %s, %s)
                    """
                    cur.execute(sql_query, (ticket_id, pii, 'transcription', url, lang))
                    con.commit()
                    print(f"Added record to FILES: ticket_id={ticket_id}, url={url}, lang={lang}, pii={pii}", flush=True)
                elif operation == "update":
                    sql_query = f"UPDATE CONVERSATIONS SET processed=1 WHERE conversation_id={conversation_id}"
                    cur.execute(sql_query)
                    con.commit()
                    print(f"Marking record {conversation_id} as processed", flush=True)
                else:
                    raise ValueError("Invalid operation provided")
            except Error as e:
                con.rollback()
                raise RuntimeError(f"Error while performing {operation} operation: {e}")
            finally:
                cur.close()  # Close the cursor
                con.close()  # Close the connection




def fetch_recordings():
    "Fetch recordings that we haven't already done (added to the FILES table)"
    try:
        result = execute_query("""
SELECT conversation_id, ticket_id, recordings, group_id, processed, ingest_at
FROM CONVERSATIONS
WHERE type = 'voice' AND recordings NOT LIKE '%[]%' AND processed is null
GROUP BY ticket_id
ORDER BY ingest_at DESC

        """)
    except Exception as e:
        raise RuntimeError(f"Error while fetching recordings: {e}")
    
    return result


def process_transcribe(conversation_id, ticket_id, recording, group_id, lang='english'):
    with transcribing_sema:
        try:
            print('Transcribing:', recording)
            # Extract the bucket_name and file_key for the MP3 file from the recording
            split_url = recording.split('/')
            bucket_name = split_url[2].split('.')[0]
            file_key = urllib.parse.unquote_plus('/'.join(split_url[3:]), encoding='utf-8')
            
            # Check the size of the object in S3
            object_size = get_object_size(bucket_name, file_key)
            if object_size is None or object_size == 0:
                print(f"Skipping {recording} due to zero size or error")
                return False
            
            # Save both the PII and NOPII transcriptions as separate .txt files
            pii_file_key = str(file_key).replace('.mp3', '_pii.txt')
            nopii_file_key = str(file_key).replace('.mp3', '_nopii.txt')

            pii_url = f"https://{bucket_name}.s3.amazonaws.com/{pii_file_key}"
            nopii_url = f"https://{bucket_name}.s3.amazonaws.com/{nopii_file_key}"


            # Extract the first two letters from the group_id to determine language
            lang_code = group_id[:2].lower()
            if lang_code == 'uk':
                lang = 'english'
            else:
                lang = 'None'

            # Call llm_transcribe function with the presigned URL of the S3 audio file
            transcription = llm_transcribe(bucket_name=bucket_name, file_key=file_key, lang=lang)
            if lang != 'english':
                lang = str(transcription["LANG"]).lower()
                pii_file_key = str(file_key).replace('.mp3', f'_pii_{lang}.txt')
                nopii_file_key = str(file_key).replace('.mp3', f'_nopii_{lang}.txt')
                pii_url = f"https://{bucket_name}.s3.amazonaws.com/{pii_file_key}"
                nopii_url = f"https://{bucket_name}.s3.amazonaws.com/{nopii_file_key}"

                
            s3.put_object(Bucket=bucket_name, Key=pii_file_key, Body=transcription['PII']['text'])
            print(f"Uploaded pii transcription file to S3: {pii_url}")
            s3.put_object(Bucket=bucket_name, Key=nopii_file_key, Body=transcription['NOPII'])
            print(f"Uploaded nopii transcription file to S3: {nopii_url}")
            # Insert piiurl and nopiiurl into FILES table
            handle_database_operation(operation='insert', ticket_id=ticket_id, url=pii_url, lang=lang, pii=1)
            handle_database_operation(operation='insert', ticket_id=ticket_id, url=nopii_url, lang=lang, pii=0)
            # Mark conversation as processed
            handle_database_operation(operation='update', conversation_id=conversation_id)
            
            print(f'Successfully transcribed {recording}')

        except Exception as e:
            raise Exception(f'Failed to process {recording} - Error: {e} ')
    return True


def process_recordings(recordings_result):
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = []

        for row in recordings_result:
            conversation_id = row[0]
            ticket_id = row[1]
            recordings = row[2]
            group_id = row[3]
            recording_list = json.loads(recordings)
            for recording in recording_list:
                future = executor.submit(process_transcribe, conversation_id, ticket_id, recording, group_id, transcribing_sema)
                futures.append(future)

        for future in as_completed(futures):
            try:
                result = future.result()  # block until the task completes
            except Exception as e:
                print(e)



def main():
    while True:
        recordings_result = fetch_recordings()
        if not recordings_result:
            print("No recordings found")
        else:
            process_recordings(recordings_result)

        time.sleep(300)  # Sleep for 5 minutes (300 seconds)


if __name__ == "__main__":
    main()
