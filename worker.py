import json
import os
import redis
from transformers import pipeline

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

print("Loading model...")
model = pipeline(
    "sentiment-analysis",
    model="distilbert-base-uncased-finetuned-sst-2-english"
)
print("Model ready. Waiting for jobs...")

r = redis.from_url(REDIS_URL)

while True:
    _, job_data = r.blpop("ml:jobs")
    job = json.loads(job_data)
    job_id = job["job_id"]

    results = model(job["inputs"])
    output = [{"label": res["label"], "score": float(res["score"])} for res in results]

    r.rpush(f"ml:result:{job_id}", json.dumps(output))
    r.expire(f"ml:result:{job_id}", 60)
