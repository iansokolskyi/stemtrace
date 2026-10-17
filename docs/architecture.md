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
- **Zero overhead in critical path**: fire-and-forget writes

How publishing behaves when the broker is slow or down, per broker, is in [Brokers](brokers.md).

## Server (`stemtrace server`)

- Reads events from the broker
- Builds task graphs
- Serves the web UI
- **Completely read-only**: safe for production

The server keeps what it reads in memory; see
[Event retention and server restarts](retention.md).
