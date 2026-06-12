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

The problem with Redis: `blpop` removes the job the moment it's popped. If the worker crashes before writing the result, the job is gone. No replay, no acknowledgment, no durability. Redis is also single-node — it goes down, the entire queue is wiped.

Kafka fixes this. Jobs live in a topic and are only considered done when the worker commits its offset. Crash before the commit = job replayed on restart. Kafka is also replicated by default, so a node going down doesn't lose data.

```
  Before:
  API  ──rpush──▶  Redis LIST  ──blpop──▶  Worker

  After:
  API  ──produce──▶  Kafka Topic: ml.jobs  ──consume──▶  Worker
                          │
                    partition 0: [job] [job] [job]
                    partition 1: [job] [job]
                    partition 2: [job]
                          │
                    consumer group: ml-workers
                    offset committed after result written
```

This is the highest-leverage change. It closes job loss on crash, adds replication, enables parallel consumers via partitions, and forces explicit idempotency with job_id dedup.

---

### Phase 2 — Add gRPC endpoint

The problem with REST for internal services: it's text-based, untyped, and has no contract enforcement. Two services can drift out of sync and you won't find out until runtime.

gRPC uses protobuf — a binary, typed schema both sides compile against. The contract is enforced at build time, not at 2am when a field name changes.

```
  browser / curl     ──▶  REST  ──▶  API  ──▶  queue  ──▶  Worker
  internal service   ──▶  gRPC  ──▶  API  ──▶  queue  ──▶  Worker
```

The backend doesn't change. Same queue, same workers. You're adding a second front door for internal callers who need typed contracts and lower overhead. REST stays for humans and external clients.

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

```
  Current:
  CPU usage    ──▶  autoscaler  ──▶  Worker replicas

  500 jobs in queue, worker CPU: 20%  →  HPA does nothing
```

Queue depth is a leading indicator. If there are 500 jobs and 1 worker, scale now — before CPU spikes, before latency climbs.

```
  After:
  Queue depth  ──▶  autoscaler  ──▶  Worker replicas
```

Tool: KEDA (Kubernetes Event-Driven Autoscaler). It reads Kafka consumer lag directly and scales workers based on how far behind they are.

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
