from contextlib import asynccontextmanager
from asyncio import get_event_loop
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from transformers import pipeline

model = None
executor = ThreadPoolExecutor()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global model
    model = pipeline(
        "sentiment-analysis",
        model="distilbert-base-uncased-finetuned-sst-2-english"
    )
    yield
    executor.shutdown(wait=False)

app = FastAPI(lifespan=lifespan)

class InputData(BaseModel):
    input: str

@app.get("/health")
def health():
    if model is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    return {"status": "ok"}

@app.get("/")
def index():
    return FileResponse("static/index.html")

@app.post("/predict")
async def predict(data: InputData):
    loop = get_event_loop()
    result = await loop.run_in_executor(executor, lambda: model(data.input)[0])
    return {
        "label": result["label"],
        "score": float(result["score"])
    }

app.mount("/static", StaticFiles(directory="static"), name="static")
