from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import redis
import psycopg2
import os
import hashlib

app = FastAPI(title="URL Shortener API")

# --- Connection settings (will be overridden by Docker later) ---
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_DB = os.getenv("POSTGRES_DB", "postgres")
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")

# Cache connection
redis_client = redis.Redis(host=REDIS_HOST, port=6379, db=0, decode_responses=True)

# Database connection
def get_db_connection():
    return psycopg2.connect(
        host=POSTGRES_HOST,
        database=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD
    )

# Data model for incoming requests
class URLItem(BaseModel):
    url: str

# --- 1. Create a shortened URL ---
@app.post("/shorten")
def shorten_url(item: URLItem):
    original_url = item.url
    # Create a short ID (e.g., first 6 characters of MD5 hash)
    short_id = hashlib.md5(original_url.encode()).hexdigest()[:6]
    
    # A. Save to PostgreSQL (Source of Truth)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS urls (short_id VARCHAR(10) PRIMARY KEY, original_url TEXT NOT NULL);"
        )
        cursor.execute(
            "INSERT INTO urls (short_id, original_url) VALUES (%s, %s) ON CONFLICT DO NOTHING", 
            (short_id, original_url)
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail="Database connection error")

    # B. Save to Redis (Cache for fast access)
    try:
        redis_client.set(short_id, original_url)
    except Exception:
        pass 

    return {"short_url": f"http://localhost:8000/{short_id}"}


# --- 2. Deep Health Check for the Sidecar (MOVED UP) ---
@app.get("/health")
def health_check():
    health_status = {
        "service": "URL Shortener API",
        "api_status": "UP",
        "redis_connection": "DOWN",
        "postgres_connection": "DOWN"
    }

    # Active Redis check
    try:
        if redis_client.ping():
            health_status["redis_connection"] = "UP"
    except Exception:
        pass

    # Active PostgreSQL check
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
        conn.close()
        health_status["postgres_connection"] = "UP"
    except Exception:
        pass

    # If something is down - return 503 with the report
    if "DOWN" in health_status.values():
        raise HTTPException(status_code=503, detail=health_status)

    return health_status


# --- 3. Retrieve URL and Redirect (MUST BE LAST) ---
@app.get("/{short_id}")
def redirect_to_url(short_id: str):
    # First stop: Try fast retrieval from Redis
    try:
        cached_url = redis_client.get(short_id)
        if cached_url:
            return RedirectResponse(url=str(cached_url))
    except Exception:
        pass

    # Second stop: Retrieve from PostgreSQL if not found in Cache
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT original_url FROM urls WHERE short_id = %s", (short_id,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if result:
            original_url = result[0]
            # Save it back to Redis for next time
            try:
                redis_client.set(short_id, original_url)
            except Exception:
                pass
            return RedirectResponse(url=original_url)
            
    except Exception:
        pass

    # Third stop: If the URL simply doesn't exist
    raise HTTPException(status_code=404, detail="URL not found")