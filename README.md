# stemtrace 🌿

**Zero-infrastructure Celery task flow visualizer**

[![PyPI version](https://badge.fury.io/py/stemtrace.svg)](https://badge.fury.io/py/stemtrace)
[![Python](https://img.shields.io/pypi/pyversions/stemtrace.svg)](https://pypi.org/project/stemtrace/)
[![CI](https://github.com/iansokolskyi/stemtrace/actions/workflows/ci.yml/badge.svg)](https://github.com/iansokolskyi/stemtrace/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/iansokolskyi/stemtrace/graph/badge.svg)](https://codecov.io/gh/iansokolskyi/stemtrace)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Type checked: mypy](https://img.shields.io/badge/type%20checked-mypy-blue.svg)](https://mypy-lang.org/)

---

> **Flower shows you what exists. Stemtrace shows you what happened.**

Ever stared at a failed Celery task wondering "what called this?" or "why did it retry 5 times?"

Stemtrace captures your task executions as a graph — visualize parent→child flows, see retry chains, track groups and chords, all without adding any new infrastructure. Just your existing Redis broker.

## ✨ Features

**See What Happened**
- **Task Flow Graphs** — Visualize chains, groups, and chords as interactive DAGs
- **Execution Timeline** — Track queued → started → retried → finished states
- **Arguments & Results** — Inspect inputs, outputs, and exceptions
- **Retry Chains** — Understand exactly when and why retries happened

**Canvas Support**
- **Groups & Chords** — Automatic visualization of `group()` and `chord()` patterns
- **Parent-Child Tracking** — See which task spawned which

**Production Ready**
- **Zero Infrastructure** — Uses your existing Redis broker, no database needed
- **Sensitive Data Scrubbing** — Passwords and API keys filtered automatically
- **Read-Only** — Safe for production; never modifies your task queue
- **FastAPI Integration** — Mount into your existing app with one line

## 🚀 Quick Start

### 1. Install

```bash
pip install stemtrace
```

### 2. Instrument your Celery app

```python
from celery import Celery
import stemtrace

app = Celery("myapp", broker="redis://localhost:6379/0")

# One line to enable flow tracking
stemtrace.init(app)
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
from stemtrace.server import StemtraceExtension

flow = StemtraceExtension(broker_url="redis://localhost:6379/0")
app = FastAPI(lifespan=flow.lifespan)
app.include_router(flow.router, prefix="/stemtrace")
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

stemtrace.init(
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

# Configuration also available via StemtraceConfig model:
# - max_data_size: Maximum serialized data size (default: 10KB)

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

stemtrace automatically detects and visualizes Celery canvas constructs:

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
| `STEMTRACE_BROKER_URL` | Broker connection URL | Auto-detect from Celery |
| `STEMTRACE_TTL` | Event TTL in seconds | `86400` |
| `STEMTRACE_PREFIX` | Key/queue prefix | `stemtrace` |

### Supported Brokers

| Broker | URL Scheme | Status |
|--------|------------|--------|
| Redis | `redis://`, `rediss://` | ✅ Supported |
| RabbitMQ | `amqp://`, `amqps://` | 🚧 Planned |
| Amazon SQS | `sqs://` | 🚧 Planned |

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

stemtrace offers two deployment modes depending on your needs:

| Mode | Best For | Command |
|------|----------|---------|
| **Standalone Server** | Dedicated monitoring, simple setup | `stemtrace server` |
| **FastAPI Embedded** | Single-app deployment, existing FastAPI apps | `StemtraceExtension` |

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

For high-throughput environments, run the consumer separately from the web server:

```bash
# Terminal 1: Run consumer (processes events)
stemtrace consume

# Terminal 2: Run API server (separate process, shares state via broker)
stemtrace server
```
### Option 2: FastAPI Embedded

Mount stemtrace directly into your existing FastAPI application:

```python
from fastapi import FastAPI
from stemtrace.server import StemtraceExtension

flow = StemtraceExtension(
    broker_url="redis://localhost:6379/0",
    # Optional configuration:
    # embedded_consumer=True,  # Run consumer in FastAPI process (default)
    # serve_ui=True,           # Serve React UI (default)
    # prefix="stemtrace",      # Redis key prefix
    # ttl=86400,               # Event TTL in seconds
    # max_nodes=10000,         # Max nodes to keep in memory
)
app = FastAPI(lifespan=flow.lifespan)
app.include_router(flow.router, prefix="/stemtrace")
```

Access the dashboard at `/stemtrace/` within your app.

#### With Custom Authentication

Use your existing auth middleware:

```python
from fastapi import Depends
from stemtrace.server import StemtraceExtension
from your_app.auth import require_admin

flow = StemtraceExtension(
    broker_url="redis://localhost:6379/0",
    auth_dependency=Depends(require_admin),
)
app = FastAPI(lifespan=flow.lifespan)
app.include_router(flow.router, prefix="/stemtrace")
```

Or use built-in auth helpers:

```python
from stemtrace.server import StemtraceExtension, require_basic_auth

flow = StemtraceExtension(
    broker_url="redis://localhost:6379/0",
    auth_dependency=require_basic_auth("admin", "secret"),
)
app = FastAPI(lifespan=flow.lifespan)
app.include_router(flow.router, prefix="/stemtrace")
```

#### Embedded Consumer Modes

| Mode | Use Case | Setup |
|------|----------|-------|
| Embedded | Development, simple apps | Default — consumer runs in FastAPI process |
| External | Production, high scale | Run `stemtrace consume` separately |

## 🗺️ Roadmap

### What's Working Now

- ✅ **Task flow graphs** — Visualize chains, groups, and chords as DAGs
- ✅ **Full lifecycle tracking** — PENDING → RECEIVED → STARTED → SUCCESS/FAILURE
- ✅ **Canvas awareness** — Automatic GROUP/CHORD node visualization
- ✅ **Arguments & results** — View inputs, outputs, and exceptions
- ✅ **Sensitive data scrubbing** — Passwords and API keys filtered automatically
- ✅ **Real-time updates** — WebSocket-powered live dashboard
- ✅ **FastAPI integration** — Mount into your existing app
- ✅ **Task registry** — Browse all discovered task definitions

### Coming Soon

- 🔜 **RabbitMQ support** — Use your existing RabbitMQ broker
- 🔜 **Worker monitoring** — See which worker processed each task
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
uv sync --all-extras  # Install dependencies
make check            # Run tests
```

## 📄 License

MIT — use it however you like.
