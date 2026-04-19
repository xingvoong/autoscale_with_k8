# Kubernetes ML Inference + Autoscaling System

A FastAPI sentiment analysis service deployed on Kubernetes with a decoupled API + worker architecture, Redis job queue, browser-based UI, load testing, and CPU-based autoscaling via Horizontal Pod Autoscaler (HPA).

---

## What it does

Runs DistilBERT sentiment analysis (`distilbert-base-uncased-finetuned-sst-2-english`) via a split API + worker design. The API server handles HTTP and queues jobs over Redis; separate worker pods load the model and process inference. Includes a built-in UI for predictions and load testing with configurable acceptance thresholds.

**API:**
- `GET /` — serves the UI
- `GET /health` — readiness check, returns 503 if Redis is unreachable
- `POST /predict` — accepts `{ "input": "<text>" }`, returns `{ "label": "POSITIVE"|"NEGATIVE", "score": 0.99 }`
- `POST /batch` — accepts `{ "inputs": ["text1", "text2", ...] }`, returns array of `{ "label", "score" }` (30s timeout)

**Stack:**
- FastAPI + Uvicorn (async, no ML dependencies)
- Separate ML worker: HuggingFace `transformers` + PyTorch for CPU inference
- Redis as job queue and result store
- Docker (`python:3.10-slim`), two images: `ml-api` and `ml-worker`
- Kubernetes: API Deployment + Worker Deployment + Redis Deployment + NodePort Service + two HPAs

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
| `deployment.yaml` | API deployment, 1 replica, CPU requests: 100m / limits: 250m, readiness + liveness probes |
| `worker-deployment.yaml` | Worker deployment, 1 replica, CPU requests: 200m / limits: 500m, readiness probe |
| `redis.yaml` | Redis deployment + ClusterIP service (`redis-service:6379`) |
| `service.yaml` | NodePort, port 80 → API container 8000 |
| `hpa.yaml` | Scales API pods 1→5 at 50% CPU utilization |
| `hpa-worker.yaml` | Scales worker pods 1→5 at 50% CPU utilization |

The low CPU limits are intentional — they force the HPAs to trigger under load, demonstrating autoscaling behavior.

**Health probes:**
- **API readiness** — pod only receives traffic once `/health` returns 200 (Redis reachable)
- **API liveness** — pod is restarted if `/health` stops responding (catches hangs)
- **Worker readiness** — exec probe pings Redis before the worker is considered ready

---

## Architecture

### Diagram 1 — POST /predict

Single text in, single result out.

```
  ┌────────┐
  │ Client │
  └───┬────┘
      │  POST /predict  {"input": "I love k8s"}
      ▼
  ┌─────────────┐
  │   API Pod   │  generates job_id (UUID)
  └──────┬──────┘
         │  ① rpush ml:jobs  {job_id, inputs: ["I love k8s"]}
         ▼
  ┌─────────────┐
  │    Redis    │  LIST ml:jobs
  └──────┬──────┘
         │  ② worker blpop — picks up job
         ▼
  ┌─────────────┐
  │ Worker Pod  │  model(["I love k8s"])
  └──────┬──────┘
         │  ③ rpush ml:result:{job_id}
         ▼
  ┌─────────────┐
  │    Redis    │  STRING ml:result:{job_id}  (TTL 60s)
  └──────┬──────┘
         │  ④ API blpop unblocks  (timeout: 10s)
         ▼
  ┌────────┐
  │ Client │  ←  {"label": "POSITIVE", "score": 0.99}
  └────────┘
```

---

### Diagram 2 — POST /batch

Multiple texts in, one result array out. All inputs travel as a single job and are processed in one model call.

```
  ┌────────┐
  │ Client │
  └───┬────┘
      │  POST /batch  {"inputs": ["text 1", "text 2", "text 3"]}
      ▼
  ┌─────────────┐
  │   API Pod   │  generates job_id (UUID)
  └──────┬──────┘
         │  ① rpush ml:jobs  {job_id, inputs: ["text 1", "text 2", "text 3"]}
         ▼
  ┌─────────────┐
  │    Redis    │  LIST ml:jobs  (one entry, all inputs together)
  └──────┬──────┘
         │  ② worker blpop — picks up job
         ▼
  ┌────────────────────────────────────┐
  │            Worker Pod              │
  │  model(["text 1", "text 2", "text 3"])  ← one batched call
  └──────────────────┬─────────────────┘
         │  ③ rpush ml:result:{job_id}
         ▼
  ┌─────────────┐
  │    Redis    │  STRING ml:result:{job_id}  (TTL 60s)
  └──────┬──────┘
         │  ④ API blpop unblocks  (timeout: 30s)
         ▼
  ┌────────┐
  │ Client │  ←  [{"label": "POSITIVE", "score": 0.99},
  └────────┘       {"label": "NEGATIVE", "score": 0.97},
                   {"label": "NEGATIVE", "score": 0.84}]
```

---

### Diagram 3 — Autoscaling

Both HPAs watch CPU independently and scale their own deployment.

```
  ┌───────────────────────────────┐
  │        Metrics Server         │
  │   polls CPU per pod every 15s │
  └──────────────┬────────────────┘
                 │
       ┌─────────┴──────────┐
       ▼                    ▼
  ┌──────────────┐    ┌──────────────┐
  │   API HPA    │    │  Worker HPA  │
  │  threshold   │    │  threshold   │
  │    50m CPU   │    │   100m CPU   │
  │ (50% × 100m) │    │ (50% × 200m) │
  └──────┬───────┘    └──────┬───────┘
         │ scale              │ scale
         ▼                    ▼
  ┌──────────────┐    ┌──────────────┐
  │     API      │    │    Worker    │
  │  Deployment  │    │  Deployment  │
  │  1 → 5 pods  │    │  1 → 5 pods  │
  └──────────────┘    └──────────────┘

  API pods scale with connection count.
  Worker pods scale with inference load.
```

---

### Diagram 4 — Full system

All components together: ingress, API tier, queue, worker tier.

```
  ╔══════════════════════════════════════════════════════════════╗
  ║  CLIENTS                                                     ║
  ║  [browser]      [curl]      [load test]                      ║
  ╚══════════════════════╤═══════════════════════════════════════╝
                         │ HTTP
  ╔══════════════════════╪═══════════════════════════════════════╗
  ║  INGRESS             │                                       ║
  ║            NodePort Service  :80                             ║
  ║            load balances across API pods                     ║
  ╚══════════════════════╤═══════════════════════════════════════╝
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
  ╔═══════════════════════════════════════════════════════════════╗
  ║  API TIER  (ml-api · no ML dependencies)   HPA: 1–5 pods    ║
  ║  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         ║
  ║  │   API Pod   │  │   API Pod   │  │   API Pod   │         ║
  ║  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘         ║
  ╚═════════╪════════════════╪════════════════╪════════════════╝
            └────────────────┼────────────────┘
                             │ rpush jobs / blpop results
  ╔══════════════════════════╪════════════════════════════════════╗
  ║  QUEUE                   │                                    ║
  ║               ┌──────────┴──────────┐                        ║
  ║               │        Redis        │                        ║
  ║               │  ml:jobs   (LIST)   │  ← jobs in            ║
  ║               │  ml:result (STRING) │  → results out        ║
  ║               └──────────┬──────────┘                        ║
  ╚══════════════════════════╪════════════════════════════════════╝
                             │ blpop jobs / rpush results
          ┌──────────────────┼──────────────────┐
          ▼                  ▼                  ▼
  ╔═══════════════════════════════════════════════════════════════╗
  ║  WORKER TIER  (ml-worker · DistilBERT)     HPA: 1–5 pods    ║
  ║  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         ║
  ║  │ Worker Pod  │  │ Worker Pod  │  │ Worker Pod  │         ║
  ║  │ pre-loaded  │  │ pre-loaded  │  │ pre-loaded  │         ║
  ║  └─────────────┘  └─────────────┘  └─────────────┘         ║
  ╚═══════════════════════════════════════════════════════════════╝
```

---

## Running locally

```bash
# start Redis
docker run -p 6379:6379 redis:7-alpine

# terminal 1 — API server (no ML deps)
pip install -r requirements-api.txt
uvicorn app:app --host 0.0.0.0 --port 8000

# terminal 2 — ML worker
pip install -r requirements-worker.txt
python worker.py
```

## Running on Kubernetes

```bash
# build both images inside minikube
eval $(minikube docker-env)
docker build -t ml-api:latest -f Dockerfile .
docker build -t ml-worker:latest -f Dockerfile.worker .

# enable metrics-server for HPA
minikube addons enable metrics-server

# deploy Redis first, then API and workers
kubectl apply -f redis.yaml
kubectl apply -f deployment.yaml
kubectl apply -f worker-deployment.yaml
kubectl apply -f service.yaml
kubectl apply -f hpa.yaml
kubectl apply -f hpa-worker.yaml

# watch both HPAs
kubectl get hpa --watch

# expose service locally
kubectl port-forward service/ml-api-service 8001:80
```

Then run the load test in the UI pointed at `http://localhost:8001` and watch worker pods scale up as inference CPU climbs.

---

## Acceptance thresholds (recommended)

Thresholds are based on measured baselines (sequential load, 1 worker, CPU-only DistilBERT).

### POST /predict

| Metric | Threshold |
|---|---|
| p50 latency | < 400ms |
| p99 latency | < 5000ms |
| Avg latency | < 700ms |
| Error rate | < 1% |

### POST /batch (per-input latency — ms ÷ batch size)

The UI load tester normalizes batch latency by batch size, so these thresholds are directly comparable to `/predict`.

| Batch size | p50 (measured) | p99 (measured) | Recommended p50 limit | Recommended p99 limit |
|---|---|---|---|---|
| 5 | ~280ms | ~480ms | < 400ms | < 600ms |
| 10 | ~290ms | ~420ms | < 400ms | < 600ms |
| 20 | ~310ms | ~1050ms | < 500ms | < 1500ms |

Per-input latency is similar across batch sizes — DistilBERT processes all inputs in one forward pass so larger batches amortize the overhead well. p99 rises at bs=20 due to occasional queue wait behind a long-running job.

| Metric | Threshold |
|---|---|
| p50 per-input latency | < 400ms |
| p99 per-input latency | < 1500ms |
| Avg per-input latency | < 500ms |
| Error rate | < 1% |
