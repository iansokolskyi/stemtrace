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
- **Fire-and-forget writes**: a failed send doesn't raise into the task, and the event is
  dropped, not buffered. (A Celery soft time limit that arrives during a send still reaches
  Celery; see the limitations in [Brokers](brokers.md).) Events are sent synchronously on the
  thread that triggers them: the task's thread for its lifecycle events, the sending thread for
  PENDING, and the worker's consumer loop for RECEIVED. So that thread waits for each send;
  timeouts and a circuit breaker bound the wait (name lookups under uWSGI excepted).

How publishing behaves when the broker is slow or down, and what each send costs, is in
[Brokers](brokers.md).

## Server (`stemtrace server`)

- Reads events from the broker
- Builds task graphs
- Serves the web UI
- **Completely read-only**: safe for production

The server keeps what it reads in memory; see
[Event retention and server restarts](retention.md).
