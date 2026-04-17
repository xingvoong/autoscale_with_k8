from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from transformers import pipeline

app = FastAPI()

# force lightweight inference mode (no explicit torch import in code)
model = pipeline(
    "sentiment-analysis",
    model="distilbert-base-uncased-finetuned-sst-2-english"
)

class InputData(BaseModel):
    input: str

@app.get("/")
def index():
    return FileResponse("static/index.html")

@app.post("/predict")
def predict(data: InputData):
    result = model(data.input)[0]
    return {
        "label": result["label"],
        "score": float(result["score"])
    }

app.mount("/static", StaticFiles(directory="static"), name="static")