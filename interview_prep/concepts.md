# Distributed Systems Concepts

These show up in every senior interview. Each one builds on the previous.

```
  CAP theorem         ──  the fundamental constraint
       │
  Consistency models  ──  how you operationalize that constraint
       │
  Replication         ──  how you get durability and availability
       │
  Partitioning        ──  how you get scale
       │
  Consensus           ──  how you coordinate across nodes
       │
  Idempotency         ──  how you handle retries safely
       │
  Backpressure        ──  how you handle load spikes
       │
  Observability       ──  how you see all of it
```

---

### CAP theorem

The problem: distributed systems run across multiple nodes, and nodes can lose contact with each other. When that happens, you have to make a choice.

You can only guarantee two of three:

```
         Consistency
              △
             / \
            /   \
           /     \
          /       \
    Availability ─── Partition Tolerance
```

- **Consistency** — every read gets the latest write
- **Availability** — every request gets a response
- **Partition tolerance** — system keeps working when nodes can't talk to each other

Network partitions happen. You can't opt out of P. So the real choice is CP or AP — what do you sacrifice when the network splits?

**CP** — refuse requests rather than return stale data. Banks do this. Wrong balance is worse than no balance.
**AP** — return stale data rather than fail. DNS does this. A slightly outdated record is better than no record.

Neither is better. Pick based on what's worse for your use case — wrong data or no data.

In this system: CP. Redis goes down, the API returns 503. It refuses rather than guesses.

---

### Consistency models

The problem: once you have multiple nodes, writes on one node don't instantly appear on others. How stale can a read be?

From strongest to weakest:

```
  Strong      ──  read always returns the latest write
                  slow, expensive, hard to scale

  Causal      ──  if A caused B, everyone sees A before B
                  middle ground

  Eventual    ──  reads may be stale, but will converge
                  fast, scales well, most common
```

Strong consistency means every read has to coordinate across nodes to confirm it's seeing the latest write. That costs latency. Eventual consistency means replicas can lag — you trade staleness for speed.

Most systems are eventually consistent by default. Know which one you're building and why.

In this system: strong. Redis is single-node. There are no replicas to lag. Every read sees the latest write.

---

### Fault tolerance

The problem: nodes fail, networks drop packets, processes crash. The question isn't if — it's when.

```
  Failure types:
  ┌────────────────┬──────────────────────────────────────┐
  │ Crash          │ node stops, no response               │
  │ Omission       │ node drops messages silently          │
  │ Timing         │ node responds, but too slowly         │
  │ Byzantine      │ node responds with wrong data         │
  └────────────────┴──────────────────────────────────────┘
```

Crash and timing are the common ones. Byzantine (lying nodes) requires expensive consensus — mostly relevant in adversarial environments like blockchains.

A slow node is more dangerous than a dead one. A dead node fails fast — the caller gets an error and moves on. A slow node holds connections open. Everything backs up waiting for it. This system has no circuit breaker, so a slow worker takes down the API with it.

---

### Replication

The problem: a single node is a single point of failure. It goes down, your data goes with it.

The fix: run multiple copies of the data across nodes.

```
  Leader ──▶ Replica 1
         ──▶ Replica 2
         ──▶ Replica 3
```

Two purposes: durability (one node dies, data survives) and read throughput (spread reads across replicas instead of hammering one node).

Two strategies for keeping replicas in sync:

```
  Synchronous:                    Asynchronous:

  leader waits for replicas       leader acks immediately
  before acking write             replicates in background

  ✓ strong consistency            ✓ low latency
  ✗ higher latency                ✗ replicas can lag
                                  ✗ data loss on leader crash
```

The tradeoff is latency vs durability. Most production systems use async replication with tunable guarantees — you pick how many replicas must confirm before the write is ack'd.

In this system: no replication. Single Redis. It dies, the queue and all results are gone.

---

### Partitioning

The problem: one node can't store or handle everything. Data gets too big, or too hot — too many reads and writes for one machine.

The fix: split the data across nodes. Each node owns a slice.

```
  user_id % 3 = 0  ──▶  Node A
  user_id % 3 = 1  ──▶  Node B
  user_id % 3 = 2  ──▶  Node C
```

The partition key determines distribution. Pick a bad key and you get a hot partition — one node drowning while the others sit idle:

```
  Bad key (skewed data):             Good key (round-robin):

  Node A: ████████████████           Node A: █████
  Node B: ██                         Node B: █████
  Node C: █                          Node C: █████
```

Round-robin distributes evenly but loses ordering. Keyed partitioning preserves ordering but risks hot spots. Which matters more depends on the workload.

In this system: partitioned by concern, not by data. API tier handles HTTP. Worker tier handles inference. Single Redis holds everything — no horizontal data split.

---

### Consensus

The problem: two nodes both think they're the leader. Both accept writes. The network heals and you have two conflicting histories. No way to know which is correct. This is split-brain.

```
  Network partition:

  [Node A] ─────✗───── [Node B]

  A thinks B is dead → "I'm leader"
  B thinks A is dead → "I'm leader"
  Both accept writes
  Partition heals → two conflicting histories
```

The fix: a node only becomes leader if a majority votes for it. With 5 nodes, majority is 3. Two nodes can't both claim 3 votes out of 5 simultaneously — two leaders is impossible.

```
  5 nodes — majority = 3

  Node A wins 3 votes  ──▶  becomes leader
  Node B wins 2 votes  ──▶  stays follower

  During partition, the minority side (< 3 nodes) refuses writes.
  Split-brain impossible.
```

You don't implement this yourself. But it's the mechanism underneath every distributed database, queue, and coordination service you use. When Kafka elects a partition leader, that's consensus. When your database fails over to a replica, that's consensus.

---

### Idempotency

The problem: networks retry. Queues redeliver. You can't guarantee a message is delivered exactly once — only at least once. Duplicates happen. If your operation isn't idempotent, duplicates cause bugs.

Charging a card twice is not idempotent. Reading a record is.

```
  Idempotent:      read a record         (same result every time)
  Idempotent:      set a value to 10     (second set is a no-op)
  Not idempotent:  charge a card         (second call = second charge)
  Not idempotent:  append to a log       (second call = duplicate entry)
```

The fix: track a unique ID per operation. Check before processing.

```
  job arrives
      │
  seen this job_id?  ──▶  yes  ──▶  skip
      │
      no
      │
  process + mark job_id as seen
```

In this system: inference is idempotent by accident — same text in, same label out. The model is deterministic. But the system doesn't track job IDs or skip reprocessing. It works by luck. For anything with side effects, that luck runs out.

---

### Backpressure

The problem is a mismatch in speed. The producer accepts requests way faster than the consumer can process them. Nothing connects those two speeds.

So when traffic spikes:

```
  API: accepting 1000 requests/min
  Worker: processing 60 requests/min
  Queue: growing by 940 jobs/min
```

Nobody stops this. The queue grows until Redis runs out of memory and crashes — and now you've lost everything in the queue on top of being overloaded. The client has no idea any of this is happening. They get a 200, think the request is fine, and the job sits in a queue for hours.

Backpressure is the signal that propagates the overload upstream. Instead of silently falling apart, the system tells the client "I'm full, slow down." The client can retry later. That's a recoverable situation. OOM is not.

**What it does:** slows the producer down before the system breaks. Check queue depth before enqueuing. If it's too deep, reject with 429. The client backs off. The queue stays bounded. The worker catches up at its own pace.

```
  No backpressure:               With backpressure:

  producer 1000/s                producer 1000/s
      │                              │
      ▼                              │  queue full → 429, slow down
  queue ▶▶▶▶ grows             ┌────▼────┐
      │       unbounded         │  queue  │  bounded
      ▼                         └────┬────┘
  OOM / crash                        │
                                consumer 100/s
```

Three ways to handle excess:
- **Drop** — discard requests, return error. Fast. Lossy.
- **Buffer** — queue the excess. Absorbs spikes. Risks unbounded growth.
- **Block** — slow the producer. Preserves all work. Propagates pressure upstream.

Which one you pick depends on whether losing work is acceptable.

---

### Observability

The problem: distributed systems fail in ways you can't predict. When something breaks at 2am, you need to know what happened, where, and why — without SSH-ing into every pod.

Three signals:

```
  Metrics  ──  numbers over time (queue depth, p99 latency, error rate)
  Traces   ──  a single request's path across every service
  Logs     ──  what happened and when, per event
```

They answer different questions:

```
  Metrics  →  p99 latency spiked from 400ms to 9s at 1:47am
                    │
  Traces   →  the spike is in the worker, not the API
                    │
  Logs     →  worker is retrying job abc-123, model OOM on batch size 50
```

Metrics tell you something is wrong. Traces tell you where. Logs tell you why.

The common mistake: adding observability after something breaks. By then you're debugging blind. Instrument from day one — queue depth and p99 latency at minimum.

In this system: none of it exists. You can't answer what the queue depth is, what p99 looks like under load, or why a request timed out.
