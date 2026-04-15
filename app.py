from fastapi import FastAPI

app = FastAPI()

def model(x: str):
    return x[::-1]  # fake ML model

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/predict")
def predict(data: dict):
    text = data.get("input", "")
    return {"output": model(text)}