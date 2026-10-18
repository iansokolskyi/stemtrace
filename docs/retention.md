# Event retention and server restarts

stemtrace builds the UI from **events** and keeps state in an **in-memory** graph store.
That means a `stemtrace server` restart starts from an empty store and only becomes "full"
again once events are re-consumed.

The store holds at most `max_nodes` tasks: 10,000 for `stemtrace server`, and the same by
default for an embedded app, which can change it with `init_app(max_nodes=...)` (see
[Embedded server options](configuration.md#embedded-server-options-init_app)).

When it is full it evicts the oldest tasks down to 90% of `max_nodes`. A group or chord goes
with all its members (and a chord with its callback), and is as old as its newest member, so a
group is never shown with part of its members and a state worked out from them; one eviction
can then free more than 10%. A group bigger than 90% of `max_nodes` can't be held whole: its
oldest members are evicted like single tasks, and its state comes from the members still held.

## Redis (Streams)

On restart, the server can rebuild state by replaying events that are
still retained in the stream (bounded by `ttl` / stream trimming).

How workers publish to Redis, and what happens when Redis is slow or down, is in
[Brokers: Redis](brokers.md#redis-streams).

## RabbitMQ (fanout + per-consumer queue)

- Events already consumed/acked by the server are **gone**.
- Events published while the server is down are only visible after restart if the server's
  durable per-consumer queue still exists and the messages are still within TTL.

How workers publish to RabbitMQ is in [Brokers: RabbitMQ](brokers.md#rabbitmq-fanout--per-consumer-queue).

## Workers and registry

The Workers and Registry pages are filled from Celery inspect on demand, so they work even if
the server missed `worker_ready` events. See [Workers and registry tabs](features.md#workers-and-registry-tabs).

## Durable history across restarts

**If you need durable history across restarts**: point stemtrace events at Redis even if your
Celery broker is RabbitMQ (set `transport_url` in `stemtrace.init_worker(...)` and set
`STEMTRACE_TRANSPORT_URL` for the server / embedded setup).
