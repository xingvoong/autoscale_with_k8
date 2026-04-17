# Kubernetes ML Inference + Autoscaling System

A FastAPI sentiment analysis service deployed on Kubernetes with a browser-based UI, load testing, and CPU-based autoscaling via Horizontal Pod Autoscaler (HPA).

---

## What it does

Runs DistilBERT sentiment analysis (`distilbert-base-uncased-finetuned-sst-2-english`) as a REST API on Kubernetes. Includes a built-in UI for predictions and load testing with configurable acceptance thresholds.

**API:**
- `GET /` — serves the UI
- `POST /predict` — accepts `{ "input": "<text>" }`, returns `{ "label": "POSITIVE"|"NEGATIVE", "score": 0.99 }`

**Stack:**
- FastAPI + Uvicorn (multi-worker)
- HuggingFace `transformers` + PyTorch for CPU inference
- Docker (`python:3.10-slim`, port 8000)
- Kubernetes Deployment + NodePort Service + HPA

---

## UI

Open `http://localhost:8000` after starting the server.

**Sentiment Prediction** — type text, get label + confidence score.

**Load Test** — browser-based load generator (no k6 required):
- Configurable VUs (virtual users), duration, per-request delay, and payload text
- Live stats: total requests, peak req/sec, avg / p50 / p99 latency, error rate
- Real-time throughput chart

**Acceptance Thresholds** — set p50, p99, avg latency, and error rate limits. Saved to localStorage. After each load test, stat boxes turn green/red and a verdict shows ACCEPTED or NOT ACCEPTED with per-check deltas (e.g. `1.9× over limit`).

---

## Kubernetes setup

| File | Purpose |
|---|---|
| `deployment.yaml` | 1 replica, CPU requests: 200m / limits: 500m |
| `service.yaml` | NodePort, port 80 → container 8000 |
| `hpa.yaml` | Scales 1→5 pods at 50% CPU utilization |

The low CPU limit is intentional — it forces the HPA to trigger under load, demonstrating autoscaling behavior.

---

## Architecture

```
User Traffic → Kubernetes Service (NodePort)
             → Pods (FastAPI inference workers)
             → Metrics Server
             → HPA → scale up/down pods
```

---

## Running locally

```bash
# install dependencies
pip install -r requirements.txt

# run with 3 workers (simulates multiple pods)
uvicorn app:app --host 0.0.0.0 --port 8000 --workers 3
```

## Running on Kubernetes

```bash
# build image inside minikube
eval $(minikube docker-env)
docker build -t ml-api:latest .

# enable metrics-server for HPA
minikube addons enable metrics-server

# deploy
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml
kubectl apply -f hpa.yaml

# watch autoscaling in action
kubectl get hpa ml-api-hpa --watch

# expose service locally
kubectl port-forward service/ml-api-service 8001:80
```

Then run the load test in the UI pointed at `http://localhost:8001` and watch pods scale up.

---

## Acceptance thresholds (recommended)

| Metric | Low load (1 VU) | Under load (3–5 VUs) |
|---|---|---|
| p50 latency | < 150ms | < 350ms |
| p99 latency | < 400ms | < 1500ms |
| Avg latency | < 200ms | < 500ms |
| Error rate | < 1% | < 5% |

CPU-only DistilBERT inference takes ~150–300ms per request. Latency scales with concurrency — HPA reduces p99 by distributing load across pods.
