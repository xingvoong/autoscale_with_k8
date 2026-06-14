# Extension Plan & Principles Mapping

---

## Project → principles mapping

```
  Principle         Status       Where in the project
  ──────────────────────────────────────────────────────────────────
  CAP theorem       ✓            CP — 503 on Redis failure, not stale data
  Consistency       ✓            Strong — Redis single-node, read = latest write
  Fault tolerance   ~            Health probes work. Crash and timing failures don't.
  Replication       ✗            Single Redis. Single worker. No durability.
  Partitioning      ~            By concern (API vs worker). Not by data.
  Consensus         ✗            Not needed yet. Single Redis, no leader election.
  Idempotency       ~            Accidental — inference is idempotent by nature.
  Backpressure      ✗            Queue grows unbounded. No 429, no limit.
  Observability     ✗            No metrics, no traces, no structured logs.
```

### What's actually implemented

**CAP — CP**
Redis goes down → `/health` returns 503 → Kubernetes marks pods unready → no traffic routed. The system refuses rather than serves stale or wrong data. That's a conscious CP choice.

**Strong consistency**
Redis operations are atomic. `rpush` and `blpop` on the same list are serialized. No two workers pop the same job. Result written by worker is immediately visible to the API's `blpop`. No staleness window.

---

### What's partial

**Fault tolerance — crash handled at infra level, not app level**

```
  What works:
  Pod crashes  ──▶  Kubernetes restarts it  ──▶  readiness probe gates traffic

  What doesn't:
  Worker crashes mid-job  ──▶  job is gone
  Worker is slow          ──▶  connections pile up, no circuit breaker
```

**Partitioning — by concern, not by data**

```
  API tier    ──  handles HTTP, no ML                (concern partition)
  Worker tier ──  handles inference, no HTTP         (concern partition)

  Missing:
  Data partition  ──  single Redis, single shard, no horizontal data split
```

**Idempotency — accidental, not designed**

```
  Same text in  ──▶  same label out   (model is deterministic)

  This works by luck. The system doesn't:
  - track job_ids to detect duplicates
  - skip reprocessing on replay
  - handle the case where a side-effectful operation runs twice
```

---

### What's missing and why it matters

**Replication — single points of failure everywhere**

```
  Redis:   one instance. Dies → queue gone, results gone.
  Worker:  scales to 2 for throughput, not durability. Both pods on same node = both go down together.
```

Fix: Redis replication (one primary, one replica). Worker anti-affinity rules to spread pods across nodes.

**Backpressure — unbounded queue**

```
  Load spike: 10,000 requests/minute
  Worker capacity: 60 requests/minute

  ml:jobs grows to 10,000 entries
  Redis memory climbs
  No signal to clients to slow down
  Eventually: OOM or multi-hour queue drain
```

Fix: check queue depth before enqueuing. Return 429 if depth exceeds threshold. Let the client back off.

**Observability — flying blind**

```
  Right now, you can't answer:
  - What's the current queue depth?
  - What's the p99 latency under load?
  - Which requests timed out and why?
  - When did the worker last restart?
```

Fix: expose queue depth as a metric, instrument every endpoint with latency histograms, add structured logs with job_id on every worker event.

---

## Extension plan

### Phase 1 — Replace Redis queue with Kafka

The problem with Redis: `blpop` removes the job the moment the worker pops it. If the worker crashes before writing the result, the job is gone. The API times out, the client gets a 504, and there's no way to recover.

Kafka keeps the job in the topic until the worker says it's done. That's the offset commit. The worker reads the job, runs inference, writes the result to Redis, then commits. Crash before the commit — job gets redelivered on restart.

```
Before:
┌─────┐        ┌───────┐        ┌────────┐
│ API │─rpush─▶│ Redis │─blpop─▶│ Worker │
└─────┘        └───────┘        └────────┘
               │
               └──▶ ❌ JOB GONE ON POP

After:
┌─────┐          ┌───────┐          ┌────────┐
│ API │─produce─▶│ Kafka │─consume─▶│ Worker │
└─────┘          └───────┘          └────────┘
                     │                   │
                     │              write result
                     │              commit offset
                     │                   │
               ✅ JOB STAYS    ✅ JOB DONE HERE
```

**Status: done.** Tested locally with Kafka in Docker. Sent a request, got back a result. The offset commit is the last line in the worker loop.

---

### Phase 2 — Add gRPC endpoint

The problem with REST for internal services: it's text-based, untyped, and has no contract enforcement. Two services can drift out of sync and you won't find out until runtime.

gRPC uses protobuf — a binary, typed schema both sides compile against. The contract is enforced at build time, not at 2am when a field name changes.

```
REST (humans/external):
┌────────┐          ┌─────────┐
│ client │─── HTTP ─▶│ app.py  │
└────────┘          └────┬────┘
                         │
gRPC (internal services):
┌────────┐          ┌───────────────┐
│ client │─── gRPC ─▶│ grpc_server  │
└────────┘          └────┬──────────┘
                         │
                    produce to Kafka
                         │
                    ┌─────────┐
                    │  Kafka  │
                    └────┬────┘
                         │
                    ┌─────────┐
                    │ Worker  │
                    └────┬────┘
                         │
                    write result
                    commit offset
                         │
                    ┌─────────┐
                    │  Redis  │
                    └─────────┘
```

The backend doesn't change. Same Kafka topic, same worker. You're adding a second front door for internal callers who need typed contracts and lower overhead. REST stays for humans and external clients.

**Status: done.** `predict.proto` defines the contract. `grpc_server.py` runs on port 50051. Same Kafka producer and Redis result retrieval as `app.py`.

---

### Phase 3 — Async result retrieval

The problem: every in-flight request holds a connection open while it waits. Under load, that becomes a resource exhaustion problem — not a worker problem.

```
  Current:
  POST /predict  ──▶  [holds connection 10s]  ──▶  result

  100 slow workers × 100 concurrent clients = connection pool gone
```

The fix is to decouple request acceptance from result delivery. Return a job_id immediately, let the client poll.

```
  After:
  POST /predict  ──▶  { "job_id": "abc-123" }   (instant)
  GET /result/abc-123  ──▶  202 pending
  GET /result/abc-123  ──▶  200 + result
```

Worker latency no longer affects API availability. A slow worker just means the client polls a few more times — not that the API runs out of connections.

---

### Phase 4 — Scale on queue depth

The problem with CPU as the autoscale signal: it's lagging. A worker between jobs shows low CPU even if there are 500 jobs waiting. The HPA sees "looks fine" and does nothing. The queue keeps growing.

Why we need to scale: one worker can only process so many jobs at a time. If 500 jobs arrive and you have 1 worker doing 60 jobs/minute — the queue grows faster than it drains. Clients wait longer and longer. Eventually timeouts, 504s, backed-up queue. Scale to 5 workers — now you're doing 300 jobs/minute. Queue drains. Latency drops.

The point of KEDA is to scale before it gets bad. CPU scales after the worker is already struggling. Queue depth scales the moment jobs start piling up — before CPU even has a chance to spike.

KEDA is Kubernetes Event-Driven Autoscaler. The built-in HPA only knows about CPU and memory. KEDA extends that to external metrics — Kafka consumer lag, Redis queue length, or any custom metric that actually reflects your workload.

```
Before (CPU-based HPA):

┌───────┐        ┌────────┐        ┌─────┐
│ Kafka │        │ Worker │─CPU %─▶│ HPA │
└───────┘        └────────┘        └──┬──┘
    │                                  │
    │  500 jobs waiting                │ "CPU 20%, looks fine"
    │                                  │
    └──────────────────────────────────▶ ❌ NO SCALE


After (KEDA + queue depth):

┌───────┐  lag   ┌──────┐  replicas  ┌────────┐
│ Kafka │───────▶│ KEDA │───────────▶│ Worker │
└───────┘        └──────┘            └────────┘
    │
    │  5+ jobs waiting
    │
    └──▶ ✅ SCALE NOW
```

Tool: KEDA reads Kafka consumer lag directly and scales workers based on how far behind they are. Configured via `keda-worker-scaler.yaml` — `lagThreshold: "5"` means scale up when more than 5 jobs are waiting.

**Status: done.** KEDA installed in cluster. `keda-worker-scaler.yaml` created with `lagThreshold: 5` and `maxReplicaCount: 5`.

---

### The extension plan closes these gaps

```
  Gap                        Closed by
  ──────────────────────────────────────────────────
  Job loss on crash          Phase 1 (durable queue, offset commit)
  Unbounded queue            Phase 1 (queue depth metric) + backpressure logic
  No replication             Phase 1 (Kafka is replicated by default)
  Accidental idempotency     Phase 1 (explicit job_id dedup in worker)
  No observability           Phase 4 (queue depth metric drives autoscaler — forces instrumentation)
  Partitioning               Phase 1 (Kafka partitions = parallel consumers)
  Consensus                  N/A — Kafka handles it internally
```
