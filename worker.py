import json
import os
import redis
from confluent_kafka import Consumer, KafkaError
from transformers import pipeline

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
KAFKA_BROKER = os.getenv("KAFKA_BROKER", "localhost:9092")

print("Loading model...")
model = pipeline(
    "sentiment-analysis",
    model="distilbert-base-uncased-finetuned-sst-2-english"
)
print("Model ready. Waiting for jobs...")

r = redis.from_url(REDIS_URL)

consumer = Consumer({
    "bootstrap.servers": KAFKA_BROKER,
    "group.id": "ml-workers",
    "auto.offset.reset": "earliest",
    "enable.auto.commit": False,
})
consumer.subscribe(["ml.jobs"])

while True:
    msg = consumer.poll(timeout=1.0)
    if msg is None or msg.error():
        continue

    job = json.loads(msg.value())
    job_id = job["job_id"]

    results = model(job["inputs"])
    output = [{"label": res["label"], "score": float(res["score"])} for res in results]

    r.rpush(f"ml:result:{job_id}", json.dumps(output))
    r.expire(f"ml:result:{job_id}", 60)

    consumer.commit(message=msg)
