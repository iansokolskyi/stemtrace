# stemtrace 🌿

**Zero-infrastructure Celery task flow visualizer**

[![Release](https://img.shields.io/badge/release-v0.2.1-darklime)](https://github.com/iansokolskyi/stemtrace/releases/tag/v0.2.1)
[![PyPI version](https://img.shields.io/pypi/v/stemtrace?color=darklime)](https://pypi.org/project/stemtrace)
[![Python](https://img.shields.io/pypi/pyversions/stemtrace.svg)](https://pypi.org/project/stemtrace/)
[![CI](https://github.com/iansokolskyi/stemtrace/actions/workflows/ci.yml/badge.svg)](https://github.com/iansokolskyi/stemtrace/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/iansokolskyi/stemtrace/graph/badge.svg)](https://codecov.io/gh/iansokolskyi/stemtrace)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Type checked: mypy](https://img.shields.io/badge/type%20checked-mypy-blue.svg)](https://mypy-lang.org/)

---

> **Flower shows you what exists. Stemtrace shows you what happened.**

Ever stared at a failed Celery task wondering "what called this?" or "why did it retry 5 times?"

Stemtrace captures your task executions as a graph — visualize parent→child flows, see retry chains, track groups and chords, all without adding any new infrastructure.

**Today stemtrace supports Redis** for event transport (via Redis Streams). **RabbitMQ support is planned.**

## ✨ Features

**See What Happened**
- **Task Flow Graphs** — Visualize chains, groups, and chords as interactive DAGs
- **Execution Timeline** — Track queued → started → retried → finished states
- **Arguments & Results** — Inspect inputs, outputs, and exceptions
- **Retry Chains** — Understand exactly when and why retries happened

**Canvas Support**
- **Groups & Chords** — Automatic visualization of `group()` and `chord()` patterns
- **Parent-Child Tracking** — See which task spawned which

**Worker Monitoring & Registry**
- **Workers page** — See which workers are online/offline and what tasks they have registered
- **Registry status badges** — Quickly spot tasks that are active, never run, or not registered by any current worker

**Production Ready**
- **Zero Infrastructure** — Uses your existing Redis broker, no database needed
- **Sensitive Data Scrubbing** — Passwords and API keys filtered automatically
- **Read-Only** — Safe for production; never modifies your task queue
- **FastAPI Integration** — Mount into your existing app with one line

## 🔎 What you’ll see in the dashboard

### Task details (timing, inputs/outputs, errors)

- **What you’ll see**: Per-task execution timing (including how long it spent in each state), parameters (args/kwargs), return value, and the full event history.
- **Why it helps**: Quickly answer “what happened?” for a single task: slow queueing vs slow execution, which retry succeeded, and (on failures) the exception + traceback for debugging.

<p align="center">
  <img src="docs/screenshots/task_details.png" width="900" alt="Task detail view showing parameters, result, and timeline" />
</p>

### Flow graphs (chains, groups, chords)

- **What you’ll see**: An interactive DAG of your workflow with parent→child edges, plus clear GROUP/CHORD containers for Celery canvas patterns.
- **Why it helps**: Understand fan-out/fan-in at a glance (especially chords), spot which branch failed, and debug “why didn’t my callback run?” without grepping logs.

<p align="center">
  <img src="docs/screenshots/workflow.png" width="900" alt="Chord visualization in the workflow graph" />
</p>

### Task registry (registration status + warnings)

- **What you’ll see**: A registry of tasks with status badges like **Active**, **Never Run**, and **Not Registered** plus “registered by …” worker info.
- **Why it helps**: Catch misconfigurations where tasks get stuck in **PENDING** because no current worker has the task registered (common in multi-repo or deploy drift scenarios).

<p align="center">
  <img src="docs/screenshots/unregistered.png" width="900" alt="Task registry showing not-registered warning and status badges" />
</p>

## 🚀 Quick Start

### 1. Install

```bash
# Using pip
pip install stemtrace

# Using uv
uv add stemtrace
```


### 2. Instrument your Celery app

```python
from celery import Celery
import stemtrace

app = Celery("myapp", broker="redis://localhost:6379/0")

# One line to enable event capture.
# Tip: put this in the module where you define your Celery app so it's imported by
# both Celery workers and any code that calls app.send_task()/delay().
stemtrace.init_worker(app)
```

### 3. View the dashboard

**Option A: Standalone server** (new container/process)

```bash
stemtrace server
```

Open [http://localhost:8000](http://localhost:8000).

**Option B: Embed in your FastAPI app** (no extra container)

```python
from fastapi import FastAPI
import stemtrace

app = FastAPI(lifespan=my_lifespan)  # Your existing app

stemtrace.init_app(app, broker_url="redis://localhost:6379/0")
```

Access at `/stemtrace/` in your existing app — no new services to deploy.

See [Deployment Options](#️-deployment-options) for auth, scaling, and more.

## 📦 Architecture

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

### Library (`stemtrace`)
- Hooks into Celery signals
- Captures task lifecycle events
- Sends normalized events to the broker
- **Zero overhead in critical path** — fire-and-forget writes

### Server (`stemtrace server`)
- Reads events from the broker
- Builds task graphs
- Serves the web UI
- **Completely read-only** — safe for production

## 🔧 Configuration

### Library Options

```python
import stemtrace

stemtrace.init_worker(
    app,
    # Optional: override broker URL (defaults to Celery's broker_url)
    transport_url="redis://localhost:6379/0",
    prefix="stemtrace",                        # Key/queue prefix
    ttl=86400,                                 # Event TTL in seconds (default: 24h)

    # Data capture (all enabled by default)
    capture_args=True,                         # Capture task args/kwargs
    capture_result=True,                       # Capture return values

    # Sensitive data scrubbing (Sentry-style)
    scrub_sensitive_data=True,                 # Scrub passwords, API keys, etc.
    additional_sensitive_keys=frozenset({"my_secret"}),  # Add custom keys
    safe_keys=frozenset({"public_key"}),       # Never scrub these keys
)

# Introspection (after init)
stemtrace.is_initialized()   # -> True
stemtrace.get_config()       # -> StemtraceConfig
stemtrace.get_transport()    # -> EventTransport (for testing)
```

#### Sensitive Data Scrubbing

By default, stemtrace scrubs common sensitive keys from task arguments:
- Passwords: `password`, `passwd`, `pwd`, `secret`
- API keys: `api_key`, `apikey`, `token`, `bearer`, `authorization`
- Financial: `credit_card`, `cvv`, `ssn`
- Session: `cookie`, `session`, `csrf`

Scrubbed values appear as `[Filtered]` in the UI.

### Canvas Graph Visualization

`stemtrace` automatically detects and visualizes Celery canvas constructs:

```text
# Parent-spawned group: GROUP is child of parent
batch_processor
└── ┌─ GROUP ──────────┐
    │  ├── add(1, 2)   │
    │  ├── add(3, 4)   │
    │  └── add(5, 6)   │
    └──────────────────┘

# Standalone group: GROUP is a root node
┌─ GROUP ──────────┐
│  ├── add(1, 1)   │
│  ├── add(2, 2)   │
│  └── add(3, 3)   │
└──────────────────┘

# Chord: header tasks inside, callback outside with edges
┌─ CHORD ──────────┐
│  ├── add(10, 10) │──┐
│  ├── add(20, 20) │──┼──► aggregate_results
│  └── add(30, 30) │──┘
└──────────────────┘
```

- **Synthetic containers** — GROUP/CHORD nodes are always created when 2+ tasks share a `group_id`
- **Parent linking** — When spawned from a parent task, the container becomes a child of that parent
- **Chord callbacks** — Rendered outside the container with edges from each header task
- **Timing** — Each node displays start time and duration directly in the graph
- **Aggregate state** — Container shows running/success/failure based on member states

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `STEMTRACE_BROKER_URL` | Broker URL for `stemtrace server` / `stemtrace consume` and `stemtrace.init_app()` | `redis://localhost:6379/0` |

### Supported Brokers

| Broker | URL Scheme | Status |
|--------|------------|--------|
| Redis | `redis://`, `rediss://` | ✅ Supported |
| RabbitMQ | `amqp://`, `amqps://` | 🚧 Planned |

## 🐳 Docker

```bash
docker run -p 8000:8000 \
    -e STEMTRACE_BROKER_URL=redis://host.docker.internal:6379/0 \
    ghcr.io/iansokolskyi/stemtrace
```

Or with Docker Compose:

```yaml
services:
  stemtrace:
    image: ghcr.io/iansokolskyi/stemtrace
    ports:
      - "8000:8000"
    environment:
      - STEMTRACE_BROKER_URL=redis://redis:6379/0
```

## 🖥️ Deployment Options

`stemtrace` offers two deployment modes depending on your needs:

| Mode | Best For | Command |
|------|----------|---------|
| **Standalone Server** | Dedicated monitoring, simple setup | `stemtrace server` |
| **FastAPI Embedded** | Single-app deployment, existing FastAPI apps | `stemtrace.init_app(...)` |

### Option 1: Standalone Server (Recommended)

The simplest way to run stemtrace — a dedicated monitoring service:

```bash
pip install stemtrace

stemtrace server
```

Open [http://localhost:8000](http://localhost:8000) to view the dashboard.

#### Server Options

```bash
stemtrace server \
    --broker-url redis://myredis:6379/0 \
    --host 0.0.0.0 \
    --port 8000 \
    --reload  # For development
```

#### High-Scale Production Setup

Note: `stemtrace server` includes an embedded consumer today (single-process). A multi-process deployment mode is planned.
### Option 2: FastAPI Embedded

Mount stemtrace directly into your existing FastAPI application:

```python
from fastapi import FastAPI
import stemtrace

app = FastAPI(lifespan=my_lifespan)  # Your existing app with lifespan

stemtrace.init_app(app, broker_url="redis://localhost:6379/0")  # Wraps lifespan, adds /stemtrace routes
```

That's it. `init_app()` automatically:
- Wraps your existing lifespan (Sentry, DB connections, etc. keep working)
- Mounts the dashboard at `/stemtrace/`
- Starts the event consumer

#### Configuration Options

```python
import stemtrace

# Returns the underlying StemtraceExtension if you need it (optional).
extension = stemtrace.init_app(
    app,
    broker_url="redis://localhost:6379/0",
    prefix="/stemtrace",        # Mount path AND event stream prefix (normalized)
    ttl=86400,                  # Event TTL in seconds
    max_nodes=10000,            # Max nodes in memory
    embedded_consumer=True,     # Run consumer in FastAPI process
    serve_ui=True,              # Serve React dashboard
    auth_dependency=None,       # Optional auth (see below)
)
```

#### With Custom Authentication

```python
from fastapi import Depends
import stemtrace
from your_app.auth import require_admin

stemtrace.init_app(app, broker_url="redis://localhost:6379/0", auth_dependency=Depends(require_admin))
```

Or use built-in auth helpers:

```python
import stemtrace

stemtrace.init_app(
    app,
    broker_url="redis://localhost:6379/0",
    auth_dependency=stemtrace.require_basic_auth("admin", "secret"),
)
```

#### Embedded Consumer Modes

| Mode | Use Case | Setup |
|------|----------|-------|
| Embedded | Development, simple apps | Default — consumer runs in FastAPI process |
| External | Production, high scale | Planned |

## 🗺️ Roadmap

### What's Working Now

- ✅ **Task flow graphs** — Visualize chains, groups, and chords as DAGs
- ✅ **Full lifecycle tracking** — PENDING → RECEIVED → STARTED → SUCCESS/FAILURE
- ✅ **Canvas awareness** — Automatic GROUP/CHORD node visualization
- ✅ **Arguments & results** — View inputs, outputs, and exceptions
- ✅ **Sensitive data scrubbing** — Passwords and API keys filtered automatically
- ✅ **Real-time updates** — WebSocket-powered live dashboard
- ✅ **FastAPI integration** — Mount into your existing app
- ✅ **Workers page** — Monitor online/offline workers and their registered tasks
- ✅ **Task registry** — Browse discovered + registered tasks with clear status badges

### Coming Soon

- 🔜 **RabbitMQ support** — Use your existing RabbitMQ broker
- 🔜 **Anomaly detection** — Spot stuck, orphaned, or failed tasks
- 🔜 **Dashboard with stats** — Success rates, durations, failure trends
- 🔜 **OpenTelemetry export** — Send traces to Jaeger, Tempo, Datadog
- 🔜 **Webhook notifications** — Push events to your systems
- 🔜 **Data export** — Download execution history as JSON

## 🤝 Contributing

Contributions, bug reports, and feature requests are welcome! This is a community project — if stemtrace helps you debug Celery, consider helping make it better.

See our [Contributing Guide](CONTRIBUTING.md) to get started.

```bash
git clone https://github.com/iansokolskyi/stemtrace.git
cd stemtrace
uv sync --extra dev   # Install dependencies
make check            # Run tests
```

## 📄 License

MIT — use it however you like.
