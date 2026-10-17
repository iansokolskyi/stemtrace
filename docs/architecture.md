# Architecture

stemtrace is designed as two decoupled components:

```text
┌──────────────────────────────────────────────────────────────────┐
│                        Your Application                          │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐        │
│  │ Celery Worker│    │ Celery Worker│    │ Celery Worker│        │
│  │ + stemtrace  │    │ + stemtrace  │    │ + stemtrace  │        │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘        │
│         │                   │                   │                │
│         └───────────────────┼───────────────────┘                │
│                             │ events                             │
│                             ▼                                    │
│                     ┌───────────────┐                            │
│                     │    Broker     │                            │
│                     └───────┬───────┘                            │
│                             │                                    │
└─────────────────────────────┼────────────────────────────────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │    stemtrace      │
                    │  server (viewer)  │
                    │  ┌─────────────┐  │
                    │  │   Web UI    │  │
                    │  └─────────────┘  │
                    └───────────────────┘
```

## Library (`stemtrace`)

- Hooks into Celery signals
- Captures task lifecycle events
- Sends normalized events to the broker
- **Fire-and-forget writes**: a failed send doesn't raise into the task, and stemtrace doesn't
  retry or buffer the event: it is lost, or delivered without confirmation (see
  [Brokers](brokers.md)). Each event is sent synchronously on the thread that triggers it, which
  waits for the send. That is usually the task's own thread. PENDING is sent on the thread that
  sent the task. RECEIVED, REVOKED, worker start and shutdown, and failures or retries the worker
  itself reports (a lost child process, a hard time limit, a cancelled task) are sent by the
  worker's own code (its consumer loop, startup and shutdown), which under prefork runs in the
  main process, not in the pool children. Timeouts and a circuit breaker
  bound each wait, except name lookups under uWSGI.
- A Celery soft time limit that lands during a send is lost: Celery logs an error and the task
  keeps running. See the limitations in [Brokers](brokers.md).

How long a send can wait when the broker is slow or down is in [Brokers](brokers.md).

## Server (`stemtrace server`)

- Reads events from the broker
- Builds task graphs
- Serves the web UI
- **Completely read-only**: safe for production

The server keeps what it reads in memory; see
[Event retention and server restarts](retention.md).
