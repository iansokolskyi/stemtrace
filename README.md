# celery-flow 🌊

**A lightweight Celery task flow visualizer**

[![PyPI version](https://badge.fury.io/py/celery-flow.svg)](https://badge.fury.io/py/celery-flow)
[![Python](https://img.shields.io/pypi/pyversions/celery-flow.svg)](https://pypi.org/project/celery-flow/)
[![CI](https://github.com/iansokolskyi/celery-flow/actions/workflows/ci.yml/badge.svg)](https://github.com/iansokolskyi/celery-flow/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/iansokolskyi/celery-flow/graph/badge.svg)](https://codecov.io/gh/iansokolskyi/celery-flow)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Type checked: mypy](https://img.shields.io/badge/type%20checked-mypy-blue.svg)](https://mypy-lang.org/)

---

> **Flower answers "what exists". celery-flow answers "what happened".**

`celery-flow` visualizes Celery task execution flows, helping you debug complex workflows by showing task graphs, timelines, retries, and parent-child relationships.

## ✨ Features

- **Task Flow Graphs** — Visualize parent → child chains, groups, and chords as DAGs
- **Execution Timeline** — See queued → received → started → retried → finished states
- **Full Lifecycle Capture** — PENDING, RECEIVED, STARTED, RETRY, SUCCESS, FAILURE states
- **Arguments & Results** — View task inputs and outputs with sensitive data scrubbing
- **Exception Capture** — Full traceback visibility on retries and failures
- **Task Registry** — Browse all discovered task definitions
- **Correlation Tracking** — Trace requests across multiple tasks via `trace_id`
- **Retry Visibility** — Know exactly which retries happened and why
- **Broker-Agnostic** — Works with Redis, RabbitMQ, and other Celery brokers
- **FastAPI Pluggable** — Mount directly into your existing FastAPI app
- **Zero Config** — Auto-detects your Celery broker configuration
- **Read-Only** — Safe for production; never modifies your task queue

## 🚀 Quick Start

### 1. Install

```bash
pip install celery-flow
```

### 2. Instrument your Celery app

```python
from celery import Celery
from celery_flow import init

app = Celery("myapp", broker="redis://localhost:6379/0")

# One line to enable flow tracking
init(app)
```

### 3. Run the visualizer

```bash
celery-flow server
```

Open [http://localhost:8000](http://localhost:8000) and watch your task flows come alive.

> By default, connects to `redis://localhost:6379/0`. Override with `--broker-url` or `CELERY_FLOW_BROKER_URL` env var.

See [Deployment Options](#️-deployment-options) for FastAPI integration and production setups.

## 📦 Architecture

celery-flow is designed as two decoupled components:

```
┌──────────────────────────────────────────────────────────────────┐
│                        Your Application                          │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐        │
│  │ Celery Worker│    │ Celery Worker│    │ Celery Worker│        │
│  │ + celery_flow│    │ + celery_flow│    │ + celery_flow│        │
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
                    │  celery-flow      │
                    │  server (viewer)  │
                    │  ┌─────────────┐  │
                    │  │   Web UI    │  │
                    │  └─────────────┘  │
                    └───────────────────┘
```

### Library (`celery-flow`)
- Hooks into Celery signals
- Captures task lifecycle events
- Sends normalized events to the broker
- **Zero overhead in critical path** — fire-and-forget writes

### Server (`celery-flow server`)
- Reads events from the broker
- Builds task graphs
- Serves the web UI
- **Completely read-only** — safe for production

## 🔧 Configuration

### Library Options

```python
from celery_flow import init

init(
    app,
    # Optional: override broker URL (defaults to Celery's broker_url)
    transport_url="redis://localhost:6379/0",
    prefix="celery_flow",                   # Key/queue prefix
    ttl=86400,                              # Event TTL in seconds (default: 24h)
    
    # Data capture (all enabled by default)
    capture_args=True,                      # Capture task args/kwargs
    capture_result=True,                    # Capture return values
    
    # Sensitive data scrubbing (Sentry-style)
    scrub_sensitive_data=True,              # Scrub passwords, API keys, etc.
    additional_sensitive_keys=frozenset({"my_secret"}),  # Add custom keys
    safe_keys=frozenset({"public_key"}),    # Never scrub these keys
)
```

#### Sensitive Data Scrubbing

By default, celery-flow scrubs common sensitive keys from task arguments:
- Passwords: `password`, `passwd`, `pwd`, `secret`
- API keys: `api_key`, `apikey`, `token`, `bearer`, `authorization`
- Financial: `credit_card`, `cvv`, `ssn`
- Session: `cookie`, `session`, `csrf`

Scrubbed values appear as `[Filtered]` in the UI.

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `CELERY_FLOW_BROKER_URL` | Broker connection URL | Auto-detect from Celery |
| `CELERY_FLOW_TTL` | Event TTL in seconds | `86400` |
| `CELERY_FLOW_PREFIX` | Key/queue prefix | `celery_flow` |

### Supported Brokers

| Broker | URL Scheme | Status |
|--------|------------|--------|
| Redis | `redis://`, `rediss://` | ✅ Supported |
| RabbitMQ | `amqp://`, `amqps://` | 🚧 Planned |
| Amazon SQS | `sqs://` | 🚧 Planned |

## 🐳 Docker

```bash
# With Redis
docker run -p 8000:8000 \
    -e CELERY_FLOW_BROKER_URL=redis://host.docker.internal:6379/0 \
    ghcr.io/celery-flow/server

# With RabbitMQ
docker run -p 8000:8000 \
    -e CELERY_FLOW_BROKER_URL=amqp://guest:guest@host.docker.internal:5672/ \
    ghcr.io/celery-flow/server
```

Or with Docker Compose:

```yaml
services:
  celery-flow:
    image: ghcr.io/celery-flow/server
    ports:
      - "8000:8000"
    environment:
      - CELERY_FLOW_BROKER_URL=redis://redis:6379/0
```

## 🖥️ Deployment Options

celery-flow offers two deployment modes depending on your needs:

| Mode | Best For | Command |
|------|----------|---------|
| **Standalone Server** | Dedicated monitoring, simple setup | `celery-flow server` |
| **FastAPI Embedded** | Single-app deployment, existing FastAPI apps | `CeleryFlowExtension` |

### Option 1: Standalone Server (Recommended)

The simplest way to run celery-flow — a dedicated monitoring service:

```bash
pip install celery-flow

celery-flow server
```

Open [http://localhost:8000](http://localhost:8000) to view the dashboard.

#### Server Options

```bash
celery-flow server \
    --broker-url redis://myredis:6379/0 \
    --host 0.0.0.0 \
    --port 8000 \
    --reload  # For development
```

#### High-Scale Production Setup

For high-throughput environments, run the consumer separately from the web server:

```bash
# Terminal 1: Run consumer (processes events)
celery-flow consume

# Terminal 2: Run API server (separate process, shares state via broker)
celery-flow server
```

### Option 2: FastAPI Embedded

Mount celery-flow directly into your existing FastAPI application:

```python
from fastapi import FastAPI
from celery_flow.server import CeleryFlowExtension

flow = CeleryFlowExtension(broker_url="redis://localhost:6379/0")
app = FastAPI(lifespan=flow.lifespan)
app.include_router(flow.router, prefix="/celery-flow")
```

Access the dashboard at `/celery-flow/` within your app.

#### With Custom Authentication

Use your existing auth middleware:

```python
from fastapi import Depends
from celery_flow.server import CeleryFlowExtension
from your_app.auth import require_admin

flow = CeleryFlowExtension(
    broker_url="redis://localhost:6379/0",
    auth_dependency=Depends(require_admin),
)
app = FastAPI(lifespan=flow.lifespan)
app.include_router(flow.router, prefix="/celery-flow")
```

Or use built-in auth helpers:

```python
from celery_flow.server import CeleryFlowExtension, require_basic_auth

flow = CeleryFlowExtension(
    broker_url="redis://localhost:6379/0",
    auth_dependency=require_basic_auth("admin", "secret"),
)
app = FastAPI(lifespan=flow.lifespan)
app.include_router(flow.router, prefix="/celery-flow")
```

#### Embedded Consumer Modes

| Mode | Use Case | Setup |
|------|----------|-------|
| Embedded | Development, simple apps | Default — consumer runs in FastAPI process |
| External | Production, high scale | Run `celery-flow consume` separately |

## 🗺️ Roadmap

- [x] Task lifecycle tracking via signals
- [x] Broker-agnostic event transport (Redis Streams)
- [x] FastAPI pluggable integration
- [x] React SPA dashboard with real-time WebSocket updates
- [x] Task flow graph visualization
- [x] Execution timeline view
- [x] Task args/kwargs capture with sensitive data scrubbing
- [x] Exception and traceback capture
- [x] Task registry (browse all discovered tasks)
- [x] PENDING state capture (task_sent signal) — see queued tasks immediately
- [x] RECEIVED state capture (worker bootstep) — see when worker picks up task
- [ ] RabbitMQ transport
- [ ] Trace ID correlation
- [ ] OpenTelemetry export
- [ ] Task duration heatmaps
- [ ] Failure clustering

## 🤝 Contributing

Contributions are welcome! Please read our [Contributing Guide](CONTRIBUTING.md) first.

```bash
# Clone the repo
git clone https://github.com/iansokolskyi/celery-flow.git
cd celery-flow

# Install dependencies (requires uv)
uv sync --all-extras

# Run checks
make check
```

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

**celery-flow** is not affiliated with the Celery project. Celery is a trademark of Ask Solem.

