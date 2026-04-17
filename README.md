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

### Batch processing flow

```
                        ┌──────────────────────────────────────────────────────────────────┐
                        │                        Kubernetes Cluster                         │
                        │                                                                   │
  POST /predict         │  ┌──────────────────────────────────────────────────────────┐   │
  POST /batch     ──────┼─►│              NodePort Service :80                         │   │
                        │  └──────────────────────┬───────────────────────────────────┘   │
                        │                         │ load balances                          │
                        │              ┌──────────┴──────────┐                            │
                        │              ▼                      ▼                            │
                        │       ┌────────────┐        ┌────────────┐                      │
                        │       │  API Pod 1 │        │  API Pod 2 │   (ml-api)           │
                        │       │  FastAPI   │        │  FastAPI   │   no ML deps         │
                        │       └─────┬──────┘        └─────┬──────┘                      │
                        │             │  1. enqueue job      │                             │
                        │             │     (job_id, inputs) │                             │
                        │             ▼                      ▼                             │
                        │       ┌──────────────────────────────────┐                      │
                        │       │           Redis                   │                      │
                        │       │   LIST  ml:jobs        ◄─ rpush  │                      │
                        │       │   STRING ml:result:{id} ─► blpop │                      │
                        │       └──────────────┬───────────────────┘                      │
                        │                      │  2. blpop (blocking dequeue)              │
                        │          ┌───────────┴──────────┐                               │
                        │          ▼                       ▼                               │
                        │   ┌────────────┐        ┌────────────┐                          │
                        │   │ Worker Pod │        │ Worker Pod │   (ml-worker)            │
                        │   │ DistilBERT │        │ DistilBERT │   model pre-loaded       │
                        │   └─────┬──────┘        └─────┬──────┘                          │
                        │         │  3. run inference    │                                 │
                        │         │     all inputs       │                                 │
                        │         │  4. rpush result     │                                 │
                        │         └──────────┬───────────┘                                │
                        │                    ▼                                             │
                        │       ┌──────────────────────────────────┐                      │
                        │       │           Redis                   │                      │
                        │       │   ml:result:{job_id}  (TTL 60s)  │                      │
                        │       └──────────────┬───────────────────┘                      │
                        │                      │  5. API blpop — unblocks                  │
                        │              ┌───────┴───────┐                                  │
                        │              ▼               ▼                                   │
                        │       ┌────────────┐  ┌────────────┐                            │
                        │       │  API Pod 1 │  │  API Pod 2 │                            │
                        │       └─────┬──────┘  └─────┬──────┘                            │
                        │             └────────┬───────┘                                  │
                        │                      │  6. return JSON to client                 │
                        └──────────────────────┼──────────────────────────────────────────┘
                                               ▼
                                         HTTP response
                                    [{"label":"POSITIVE","score":0.99}, ...]
```

**How a request flows:**
1. Client sends `POST /batch` with `{ "inputs": ["text1", "text2", ...] }` (or `POST /predict` for a single input)
2. API pod assigns a UUID `job_id`, serializes the job, and pushes it onto the `ml:jobs` Redis list (`rpush`)
3. API pod blocks on `blpop ml:result:{job_id}` — waiting up to 30s (10s for `/predict`)
4. A worker pod dequeues the job via `blpop ml:jobs` (blocking pop — one job per worker at a time)
5. Worker runs all inputs through DistilBERT in a single batched call, then pushes the result JSON to `ml:result:{job_id}` with a 60s TTL
6. The waiting API pod unblocks, reads the result, and returns it to the client

**How autoscaling works:**
1. Metrics Server collects CPU usage per pod every 15s
2. API HPA scales API pods 1→5 when average CPU exceeds 50% of the 100m request (50m)
3. Worker HPA scales worker pods 1→5 when average CPU exceeds 50% of the 200m request (100m)
4. Workers are the CPU bottleneck (model inference); API pods are lightweight and scale for connection concurrency
5. New pods don't receive traffic until their readiness probes pass

### Key design properties

- **Decoupled scaling** — API and worker pods scale independently; a spike in connections scales API pods, a spike in inference load scales workers
- **No ML in the API** — `ml-api` image has no `torch`/`transformers` dependencies, keeping it small and fast to start
- **Model pre-loaded** — `ml-worker` image bakes the DistilBERT weights in at build time; workers are ready as soon as the container starts
- **Job isolation** — each request gets a unique `job_id`; results are keyed by ID so concurrent requests never collide
- **Backpressure** — if all workers are busy the Redis queue grows; clients block up to the timeout, then receive a 504

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

| Metric | Low load (1 VU) | Under load (3–5 VUs) |
|---|---|---|
| p50 latency | < 150ms | < 350ms |
| p99 latency | < 400ms | < 1500ms |
| Avg latency | < 200ms | < 500ms |
| Error rate | < 1% | < 5% |

CPU-only DistilBERT inference takes ~150–300ms per request. Latency scales with concurrency — HPA reduces p99 by distributing load across pods.
