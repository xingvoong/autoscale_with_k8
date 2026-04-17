import json
import os
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import redis.asyncio as aioredis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
redis_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_client = aioredis.from_url(REDIS_URL)
    yield
    await redis_client.aclose()

app = FastAPI(lifespan=lifespan)

class InputData(BaseModel):
    input: str

class BatchInputData(BaseModel):
    inputs: list[str]

@app.get("/health")
async def health():
    try:
        await redis_client.ping()
    except Exception:
        raise HTTPException(status_code=503, detail="redis unavailable")
    return {"status": "ok"}

@app.get("/")
def index():
    return FileResponse("static/index.html")

@app.post("/predict")
async def predict(data: InputData):
    job_id = str(uuid.uuid4())
    job = {"job_id": job_id, "inputs": [data.input]}
    await redis_client.rpush("ml:jobs", json.dumps(job))
    result = await redis_client.blpop(f"ml:result:{job_id}", timeout=10)
    if not result:
        raise HTTPException(status_code=504, detail="inference timeout")
    return json.loads(result[1])[0]

@app.post("/batch")
async def batch_predict(data: BatchInputData):
    job_id = str(uuid.uuid4())
    job = {"job_id": job_id, "inputs": data.inputs}
    await redis_client.rpush("ml:jobs", json.dumps(job))
    result = await redis_client.blpop(f"ml:result:{job_id}", timeout=30)
    if not result:
        raise HTTPException(status_code=504, detail="inference timeout")
    return json.loads(result[1])

app.mount("/static", StaticFiles(directory="static"), name="static")
