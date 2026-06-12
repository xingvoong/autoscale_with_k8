# Project — Sentiment Analysis API on Kubernetes

---

## What this system does

Sentiment analysis API. Send text, get back a label and confidence score.

The ML part is boring. The interesting part is the architecture — API and inference run in separate services, scale independently, and communicate through a queue.

---

## Architecture

```
  [browser]  [curl]  [load test]
        │
        ▼
  ┌─────────────────────┐
  │  NodePort :80        │  ← load balances across API pods
  └──────────┬──────────┘
             │
    ┌────────┼────────┐
    ▼        ▼        ▼
  [API]    [API]    [API]     HPA: 1–5 pods · no ML deps
    └────────┼────────┘
             │  rpush / blpop
             ▼
  ┌─────────────────────┐
  │        Redis         │
  │  ml:jobs   → queue  │
  │  ml:result → store  │
  └──────────┬──────────┘
             │  blpop / rpush
    ┌────────┼────────┐
    ▼        ▼        ▼
  [Worker] [Worker]         HPA: 1–2 pods · owns DistilBERT
```

Two tiers. One queue. Each tier scales on its own signal.

---

## Request flow

```
  client
    │
    │  POST /predict {"input": "I love k8s"}
    ▼
  API pod  ──── rpush ml:jobs {job_id, input} ────▶  Redis
    │                                                    │
    │  blpop ml:result:{job_id}  (blocks, 10s timeout)  │
    │◀───────────────────────────────────────────────────┘
    │                                  ▲
    │                             Worker pops job
    │                             runs DistilBERT
    │                             rpush ml:result:{job_id}
    ▼
  {"label": "POSITIVE", "score": 0.99}
```

---

## Design decisions

### 1. API knows nothing about ML

The API only touches Redis. No model. No PyTorch. No weights.

```
  app.py                          worker.py
  ──────────────────────          ──────────────────────
  rpush job to Redis     ──▶      blpop job from Redis
  blpop result from Redis ◀──     run DistilBERT
                                  rpush result to Redis
```

**Why:** inference is slow and CPU-heavy. Connections are fast and I/O-bound. They scale differently. Keep them separate or you scale the wrong thing.

**Cost:** two deployments, two images, two HPAs. Worth it.

---

### 2. Redis as the queue

`rpush` to enqueue. `blpop` to dequeue. Workers sleep until a job arrives — no polling.

**Why:** simple. Redis was already there for result storage. No extra broker to run.

**Problem:** `blpop` is destructive. Job is removed the moment it's popped. Worker crashes before writing result → job is gone. No acknowledgment step, no replay.

**Fix:** a broker that separates consumption from acknowledgment. Worker marks the job done only after writing the result. Crash before that = job replayed on restart.

---

### 3. Synchronous result retrieval

API enqueues a job, then blocks waiting for the result. One HTTP call, one response.

```
  client  ──▶  POST /predict  ──▶  [waits up to 10s]  ──▶  result
```

**Why:** simple client interface. No polling, no webhooks.

**Problem:** each in-flight request holds a connection open. Under load:

```
  100 concurrent requests
  each waiting 10s for a slow worker
  = connection pool exhausted before any timeout fires
```

**Fix:** async pattern. Return `job_id` immediately, let client poll.

```
  POST /predict  →  { "job_id": "abc-123" }    (instant)
  GET /result/abc-123  →  202 pending
  GET /result/abc-123  →  200 done + result
```

---

### 4. Autoscale on CPU

Both HPAs watch CPU. API scales 1→5 at 50% of 100m. Worker scales 1→2 at 50% of 200m.

**Why:** CPU is the right proxy here. API uses CPU under connection load. Worker uses CPU for inference.

**Problem:** CPU is a lagging signal for queue-based work.

```
  500 jobs in queue
  worker CPU: 20%  (between jobs, not spiked yet)
  HPA: "looks fine, no scale needed"
  queue keeps growing
```

**Fix:** scale on queue depth, not CPU. If there are 500 jobs and 1 worker — scale now.

---

### 5. Health probes

API readiness: `GET /health` → pings Redis → 503 if unreachable.
Worker readiness: `exec redis-cli ping`.

**Why:** both services depend on Redis. Without probes, Kubernetes sends traffic to pods before they're ready.

```
  Pod starts
      │
      ▼
  readiness probe fires
      │
  Redis unreachable?  ──▶  pod stays out of rotation
      │
  Redis reachable?    ──▶  pod gets traffic
```

**Trade-off:** Redis goes down → all pods fail readiness → traffic stops entirely. That's the right call. A 503 is cleaner than corrupted responses.

---

## Failure modes

### Worker crashes mid-job

The job is already removed from the queue the moment the worker pops it. `blpop` is destructive — there's no "give it back if you fail" step. So if the worker crashes before writing the result, the job is gone. The API is still blocking on `blpop ml:result:{job_id}`, waits 10 seconds, and returns 504. The client has no idea whether to retry or not.

```
  Redis                    Worker
    │                        │
    │◀── blpop (job popped) ─┤
    │                        │
    │                        ✗ crash
    │
  job is gone
  API times out → 504
```

**Fix:** Kafka. The worker commits its offset only after writing the result. Crash before the commit = job gets redelivered to the next worker on restart.

---

### Redis goes down

Redis is the single point of failure for everything — the job queue, the result store, and the health check. When it goes down, the health probe fails, Kubernetes marks all pods unready, and traffic stops.

```
  API → GET /health → ping Redis → fail → 503
  Kubernetes: pod unready, stops routing traffic
  ml:jobs queue: wiped on restart (no persistence configured)
```

The 503 is the right behavior — better to stop traffic than accept jobs that can't be processed. But any jobs in the queue at the time are lost. Redis has no persistence configured, so a restart starts clean.

**Fix:** Redis AOF persistence to survive restarts, or replace with Kafka which is durable by design.

---

### Slow worker, not dead

A crashed worker fails fast — the API gets an error and can move on. A slow worker is worse. It's still alive, still accepting jobs, but taking 9 seconds instead of 1. The API holds each connection open for the full timeout. Under load, those connections pile up.

```
  Worker responding in 9s instead of crashing
      │
  API holds connection open for 10s
      │
  100 concurrent requests × 10s = connection pool gone
      │
  No circuit breaker → API keeps accepting new requests
      │
  Everything backs up
```

There's no signal to stop. The API keeps accepting new requests and handing them to a worker that can't keep up.

**Fix:** circuit breaker. After N timeouts, return 503 immediately instead of waiting. Stop sending work to a struggling worker and give it time to recover.

---

### Duplicate processing

The API has a 10-second timeout on `blpop`. If the worker is slow, the API gives up and returns 504. The worker might finish anyway and write the result — but nobody is listening. The result sits in Redis and expires after 60 seconds.

```
  Worker writes result
  API blpop already timed out → result sits, expires in 60s
  Client retries → new job_id → job runs again
```

Safe here — inference is idempotent. Same input, same output, second result just expires.

Not safe for stateful ops. If the operation has side effects, check job_id before processing to avoid running it twice.

---

### Uneven load across workers

Workers race to pop jobs from the queue. Whoever calls `blpop` first gets the next job. This means a slow batch job and a fast single request share the same queue — and there's no priority.

```
  Worker A: processing 20-input batch (slow)
  Worker B: idle, blocked on blpop
  New small jobs arrive → go to whoever pops first
  Worker B gets them, Worker A is stuck
```

Small jobs might still get through since Worker B is free. But if Worker A is the only worker, small jobs queue behind the batch and wait.

**Fix:** separate queues for `/predict` and `/batch`. Small jobs don't get stuck behind large ones.

---

## Interview Q&A

**"Walk me through your architecture."**
Client hits the API. API enqueues a job to Redis, blocks on the result. Worker pops the job, runs inference, writes the result back. API unblocks, returns to client. Two tiers, one queue.

**"Why separate the API and worker?"**
They scale on different signals. API scales on connections. Worker scales on inference load. Same pod means scaling both when you only need one.

**"What happens if the worker crashes?"**
Current: job is gone, client gets 504. With Kafka: job stays in the topic, gets reprocessed on restart. That's the main reason to replace Redis.

**"How do you handle backpressure?"**
Right now, badly. The queue grows unbounded. Fix: reject with 429 when queue depth exceeds a threshold.

**"How do you scale to 10x traffic?"**
More API pods — HPA handles that. More partitions in the queue so more workers can consume in parallel. Switch to async retrieval to remove the connection-holding bottleneck. If CPU inference is the ceiling, move to GPU workers for the hot path.

**"What would you change?"**
Redis queue made sense to get it working fast. Kafka is the right call for production — durability, replay, consumer groups. I'd also instrument queue depth and p99 from day one. You can't tune what you can't measure.
