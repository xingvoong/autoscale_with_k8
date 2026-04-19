# Kubernetes ML Inference + Autoscaling System

Production-ready sentiment analysis service built on Kubernetes. Decoupled API and worker architecture, Redis job queue, CPU-based autoscaling via HPA, and a browser-based UI with built-in load testing.

**This covers the serving layer of an ML system** — taking a trained model and running it reliably under real traffic. Specifically:
- Model serving — exposing DistilBERT as a REST API
- Inference infrastructure — job queue, workers, and autoscaling
- Deployment — containerized on Kubernetes with health probes and HPA

It assumes the model is already trained. Data collection, training, experimentation, and monitoring are out of scope.

---

## What it does

The API handles HTTP and queues inference jobs to Redis. A separate worker picks up those jobs, runs them through DistilBERT, and writes results back. The two services scale independently. Supports single-input and batch inference.

**Endpoints:**
- `GET /` — serves the UI
- `GET /health` — returns 503 if Redis is unreachable
- `POST /predict` — accepts `{ "input": "<text>" }`, returns `{ "label": "POSITIVE"|"NEGATIVE", "score": 0.99 }`
- `POST /batch` — accepts `{ "inputs": ["text1", "text2", ...] }`, returns array of `{ "label", "score" }` (30s timeout)

**Stack:**
- FastAPI + Uvicorn — async, no ML dependencies in the API
- HuggingFace `transformers` + PyTorch (CPU) — lives only in the worker
- Redis — job queue and result store
- Two Docker images: `ml-api` and `ml-worker`
- Kubernetes: API Deployment, Worker Deployment, Redis, NodePort Service, two HPAs

---

## UI

Open `http://localhost:8000` when running locally, or `http://localhost:8001` when using Kubernetes port-forward.

There are three things you can do in the UI:

**Sentiment Prediction** — type a sentence, get back a label and confidence score.

**Batch Processing** — enter one sentence per line, get back results for all of them at once.

**Load Test** — no k6 needed. Run it directly in the browser.
- Set VUs (virtual users), duration, delay, and payload text
- Switch between real-time mode (`/predict`) and batch mode (`/batch`) with configurable batch size
- Live stats: total requests, peak req/sec, avg / p50 / p99 latency, error rate
- Real-time throughput chart
- Set acceptance thresholds for p50, p99, avg latency, and error rate. After each test you get an ACCEPTED or NOT ACCEPTED verdict with per-check deltas

---

## Kubernetes setup

Six manifest files. Each one does one thing.

| File | Purpose |
|---|---|
| `deployment.yaml` | API deployment — 1 replica, CPU requests: 100m / limits: 250m, readiness + liveness probes |
| `worker-deployment.yaml` | Worker deployment — 1 replica, CPU requests: 200m / limits: 500m, readiness probe |
| `redis.yaml` | Redis deployment + ClusterIP service (`redis-service:6379`) |
| `service.yaml` | NodePort — port 80 → API container 8000 |
| `hpa.yaml` | Scales API pods 1→5 at 50% CPU |
| `hpa-worker.yaml` | Scales worker pods 1→2 at 50% CPU |

The CPU limits are intentionally low. That's what makes the HPA trigger during load testing — it's easier to demonstrate autoscaling when the threshold is easy to hit.

**Health probes:**
- **API readiness** — pod only gets traffic once `/health` returns 200, meaning Redis is reachable
- **API liveness** — pod restarts if `/health` stops responding
- **Worker readiness** — exec probe pings Redis before the worker is considered ready

---

## Architecture

### Diagram 1 — POST /predict

One input in. One result out.

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

Multiple inputs in. One result array out. All inputs go as a single job — the worker processes them in one model call.

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

The API HPA and Worker HPA watch CPU independently. They don't know about each other.

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
  │  1 → 5 pods  │    │  1 → 2 pods  │
  └──────────────┘    └──────────────┘

  API pods scale with connection count.
  Worker pods scale with inference load.
```

---

### Diagram 4 — Full system

How all the pieces fit together.

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
  ║  WORKER TIER  (ml-worker · DistilBERT)     HPA: 1–2 pods    ║
  ║  ┌─────────────┐  ┌─────────────┐                           ║
  ║  │ Worker Pod  │  │ Worker Pod  │                           ║
  ║  │ pre-loaded  │  │ pre-loaded  │                           ║
  ║  └─────────────┘  └─────────────┘                           ║
  ╚═══════════════════════════════════════════════════════════════╝
```

---

## Running locally

You need three things running: Redis, the API, and the worker.

```bash
# start Redis
docker run -p 6379:6379 redis:7-alpine

# terminal 1 — API server
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

Open `http://localhost:8001`, run the load test, and watch worker pods scale up as inference CPU climbs.

---

## Acceptance thresholds

These are based on real measurements — sequential load, one worker, CPU-only DistilBERT. Don't use made-up numbers as thresholds. Run the test first.

### POST /predict

| Metric | Threshold |
|---|---|
| p50 latency | < 400ms |
| p99 latency | < 5000ms |
| Avg latency | < 700ms |
| Error rate | < 1% |

### POST /batch (per-input latency — ms ÷ batch size)

The UI normalizes batch latency by batch size so you can compare it directly to `/predict`. Batch is actually faster per input — the model processes all inputs in one forward pass, so the overhead is shared.

| Batch size | p50 (measured) | p99 (measured) | Recommended p50 limit | Recommended p99 limit |
|---|---|---|---|---|
| 5 | ~280ms | ~480ms | < 400ms | < 600ms |
| 10 | ~290ms | ~420ms | < 400ms | < 600ms |
| 20 | ~310ms | ~1050ms | < 500ms | < 1500ms |

p99 goes up at batch size 20 because of queue wait, not inference time. The model itself is fast — the job is just waiting behind another one.

| Metric | Threshold |
|---|---|
| p50 per-input latency | < 400ms |
| p99 per-input latency | < 1500ms |
| Avg per-input latency | < 500ms |
| Error rate | < 1% |

---

## Recap

**What was built:**
1. FastAPI sentiment analysis API with DistilBERT
2. Browser-based UI with load testing and acceptance thresholds
3. Async inference via `ThreadPoolExecutor` to keep the event loop free
4. Health probes so Kubernetes only routes traffic to ready pods
5. HPA to scale pods on CPU under load
6. Decoupled API and worker — model inference moved into its own image
7. Redis job queue: API enqueues, worker dequeues and responds
8. `POST /batch` for multi-input inference in a single round-trip
9. Measured latency baselines and set thresholds from real data

---

## Key takeaways

**Don't put things together that scale differently.** The API handles connections. The worker handles inference. They have different bottlenecks. If you keep them in the same pod, you scale both when you only need to scale one. Separate them.

**A queue is what lets two services run at their own speed.** The API doesn't wait for the worker. It drops the job and moves on. The worker picks it up when it's ready. If the worker restarts, the job is still in the queue. That's the point.

**Batch when you can.** If you have multiple inputs ready at the same time, send them together. One model call over ten inputs is faster per input than ten separate calls. You pay the network and queue cost once, not ten times.

**Real-time is for users. Batch is for work.** If someone is waiting on a screen, use `/predict`. If you're processing a list, use `/batch`. Same infrastructure, different timeout budget.

**Don't guess at latency. Measure it.** Batch per-input latency (~290ms) was faster than single requests (~650ms avg). Set your thresholds after you run the tests, not before.

**A pod that is starting is not ready.** Kubernetes will send traffic to it the moment it starts unless you configure a readiness probe. For any service that takes time to load — model weights, cache, DB connections — that probe is not optional. Without it, you get errors on every deploy.
