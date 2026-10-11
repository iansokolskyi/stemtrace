<img src="https://raw.githubusercontent.com/iansokolskyi/stemtrace/main/docs/brand/stemtrace-mark.svg" width="48" alt="stemtrace logo" />

# stemtrace 🌿

**Zero-infrastructure Celery task flow visualizer**

[![PyPI version](https://img.shields.io/badge/pypi-v0.3.10-darklime)](https://pypi.org/project/stemtrace)
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

**stemtrace supports Redis and RabbitMQ** for event transport.

## ✨ Features

**See What Happened**
- **Task Flow Graphs** — Visualize chains, groups, and chords as interactive DAGs
- **Execution Timeline** — Track queued → started → retried → finished states
- **Arguments & Results** — Inspect inputs, outputs, and exceptions
- **Task Output** — See what each attempt printed and logged, next to the task (opt-in)
- **Retry Chains** — Understand exactly when and why retries happened

**Canvas Support**
- **Groups & Chords** — Automatic visualization of `group()` and `chord()` patterns
- **Parent-Child Tracking** — See which task spawned which

**Worker Monitoring & Registry**
- **Workers page** — See which workers are online/offline and what tasks they have registered
- **Registry status badges** — Quickly spot tasks that are active, never run, or not registered by any current worker

**Production Ready**
- **Zero Infrastructure** — Uses your existing broker (Redis or RabbitMQ), no database needed
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

Tip: make sure the server is pointed at the same broker as your workers:

```bash
stemtrace server --broker-url redis://localhost:6379/0
# or:
stemtrace server --broker-url amqp://guest:guest@localhost:5672//
```

**Option B: Embed in your FastAPI app** (no extra container)

```python
from fastapi import FastAPI
import stemtrace

app = FastAPI(lifespan=my_lifespan)  # Your existing app

stemtrace.init_app(app, broker_url="redis://localhost:6379/0")
```

Access at `/stemtrace/` in your existing app — no new services to deploy.

See [Deployment Options](#deployment-options) for auth, scaling, and more.

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

    # Task output (disabled by default, see "Task Output" below)
    capture_output=False,                      # Capture what each attempt prints/logs
    max_output_size=16384,                     # Max bytes kept per attempt (the end is kept)
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

#### Task Output (print and logging)

Turn on `capture_output` on the worker to see what each task attempt printed and
logged on its task page, instead of searching interleaved worker logs:

```python
stemtrace.init_worker(app, capture_output=True)
```

```python
from celery.utils.log import get_task_logger
import logging

task_logger = get_task_logger(__name__)
log = logging.getLogger("myapp.billing")

@app.task(bind=True, max_retries=3)
def charge(self, order_id):
    print(f"charging {order_id}")           # -> charging 42
    task_logger.info("calling gateway")     # -> INFO myapp.tasks: calling gateway
    log.warning("gateway slow")             # -> WARNING myapp.billing: gateway slow
    ...
```

The task page shows an **Output** section with one block per attempt (latest
first): the output of a retried attempt is on its `RETRY` event, the final one on
`SUCCESS`/`FAILURE`.

- **What's captured**: `print()` to stdout and stderr, exactly as printed
  (whether or not Celery redirects stdout to its log), and log records
  (`logging.getLogger(...)`, `get_task_logger(...)`) emitted while the task
  runs, in one chronological stream. Records are captured when they reach the
  root logger or `celery.task`, or when a handler writes them to stdout/stderr
  after stemtrace is set up. A logger with `propagate=False` whose handlers were
  set up at worker startup (writing to the real stream or a file) isn't
  captured. Log levels still apply to log records: one your logging config
  filters out (e.g. `DEBUG` under `--loglevel=INFO`) isn't captured.
  stemtrace's own logs and Celery/kombu internals are left out.
  What the worker prints to its console or log file is unchanged (when no
  logging handlers are configured, warnings still print as Python's
  `logging.lastResort` would). One side effect: from the first task on, the root
  logger has stemtrace's handler, so `logging.getLogger().hasHandlers()` is
  `True` and a later `logging.basicConfig()` call without `force=True` does
  nothing. Configure logging at startup (or pass `force=True`). Logging
  handlers added while a task runs, ones that override `handle()`, or ones
  that write to stdout/stderr in unusual ways may show a record twice or show
  Celery/kombu lines in that task's output; handlers set up at worker startup
  don't.
- **Size**: at most `max_output_size` bytes (UTF-8) per attempt. Longer output
  keeps its end, where failures usually are, after a `[… N bytes truncated]` line.
  Output travels with the task's terminal event, so it is stored wherever events
  are: by the server (up to `max_nodes` tasks) and, with the Redis transport, in
  the stemtrace stream on your broker, which keeps the last `max(ttl, 10000)`
  events (86,400 with the default `ttl`). At 16 KB per attempt that can add up to
  more than a GB of Redis memory on busy workers, so consider a lower
  `max_output_size` or `ttl` there. It isn't sent over the UI's WebSocket; the
  task page fetches it.
- **Pools**: each task's output is collected in a `ContextVar` (per thread, and
  per greenlet under gevent/eventlet), so concurrent tasks never mix output.
  Output from threads a task starts itself isn't captured.
- **Not captured**: anything after the task ends, and output of a task whose
  worker process was killed (hard time limit, OOM), since no terminal event is
  sent from that process. Capture of a failed or retried attempt stops when
  Celery reports the failure/retry, so `on_failure`/`on_retry` output is included
  but `after_return` output is included only on success.
- **Not scrubbed**: output is free text, so the key-based scrubbing above can't
  apply. That's why it's off by default: only enable it if your tasks don't print
  or log secrets.

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

### Environment Variables (Optional)

You **do not need** these environment variables if you’re using the Python APIs:
- `stemtrace.init_worker(app, transport_url=..., prefix=..., ttl=...)`
- `stemtrace.init_app(app, broker_url=..., transport_url=...)`

They exist mainly as **convenient defaults** for:
- `stemtrace server` / `stemtrace consume`
- container/Docker setups where passing flags is awkward

If your app already uses env vars like `BROKER_URL`, `REDIS_URL`, etc., just pass them through:

```python
import os
import stemtrace

stemtrace.init_app(
    app,
    broker_url=os.environ["BROKER_URL"],
    transport_url=os.getenv("STEMTRACE_TRANSPORT_URL") or os.environ["BROKER_URL"],
)
```

| Variable | Description | Default |
|----------|-------------|---------|
| `STEMTRACE_BROKER_URL` | Celery broker URL (used for on-demand worker/registry inspection). Also used as the default for `STEMTRACE_TRANSPORT_URL`. | `redis://localhost:6379/0` |
| `STEMTRACE_TRANSPORT_URL` | Event transport URL (where stemtrace publishes/consumes events).  | Defaults to `STEMTRACE_BROKER_URL`. |
| `STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS` | Server only: show a task argument as the graph node name (see [Node Names from Task Arguments](#node-names-from-task-arguments)). Same as `--node-alias-from-arguments` / `init_app(node_alias_from_arguments=...)`. | Unset (task names are shown). |

### Supported Brokers

| Broker | URL Scheme | Status |
|--------|------------|--------|
| Redis | `redis://`, `rediss://` | ✅ Supported |
| RabbitMQ | `amqp://`, `amqps://`, `pyamqp://` | ✅ Supported |

### Event Retention & Server Restarts (Important)

stemtrace builds the UI from **events** and keeps state in an **in-memory** graph store.
That means a `stemtrace server` restart starts from an empty store and only becomes “full”
again once events are re-consumed.

- **Redis (Streams)**: On restart, the server can rebuild state by replaying events that are
  still retained in the stream (bounded by `ttl` / stream trimming).
  - Events that can't be published to Redis are dropped, not buffered on the worker. Workers
    publish through their own connection with a 2s connect timeout (per address the host name
    resolves to), 2s per read or write and no redis-py retries (`socket_timeout` and
    `socket_connect_timeout` in the broker URL apply to the server's consumer, not to
    publishing). Any reply slower than 2s counts as a failure. A single failure loses only its
    own event; three failures in a row (timeouts included) make the worker skip publishing for a
    cooldown that grows from 1s to 30s and resets on the next successful publish. So when Redis
    hangs or becomes unreachable, each worker process makes at most three bounded attempts
    (about 2s each per address, plus any attempts its other threads already had in flight) and
    then one per cooldown, instead of waiting 5s (redis-py's default) for every event.
  - An event whose reply timed out (or whose connection dropped) after it was sent counts as
    "sent but unconfirmed", not dropped: Redis may still have written it, e.g. after a stall
    longer than 2s (a slow Lua script, a fork for `BGSAVE`, a failover). If the retry after
    such failures loses its reply too, the next retry waits the full 30s. An error reply
    (e.g. `LOADING`, `NOAUTH`, `OOM`) means Redis rejected the event: it counts as dropped.
  - Workers resolve the broker's host name in a short-lived helper process (the same Python,
    so the same `/etc/hosts`, `resolv.conf` and name services) and wait at most the 2s connect
    timeout for it, so an unreachable DNS resolver costs an attempt about 2s too (it would
    otherwise block for the resolver's own timeouts, measured at 20s). Answers are cached per
    worker process for 30s and refreshed in the background, keeping the last answer while DNS
    fails, so tasks don't wait on DNS while the host has a cached address. When connecting to
    every cached address fails, the answer is refreshed right away, so after a DNS failover the
    next attempt (at the latest the first retry after the breaker's cooldown) uses the new
    address; a host that moved while its old address still accepts connections is picked up
    within the 30s cache time. TLS behavior is unchanged. Each lookup starts one process
    (about 60-80ms), at most once per host per 30s plus after failed connects; a helper that
    hangs is killed after 30s. Lookups don't run in the worker process itself because glibc's
    DNS code holds locks that `fork()` doesn't reset: a prefork child forked while a lookup
    thread was inside it could deadlock. On Windows, or where there is no Python executable to
    start (embedded interpreters), lookups run in-process instead. The same applies to
    RabbitMQ.
  - A Redis that still answers within 2s doesn't open the breaker, however slow: each publish
    waits for its reply.
  - Logging is bounded as for RabbitMQ below: at most four warnings a minute, each with the
    number of events dropped since the previous one.
- **RabbitMQ (fanout + per-consumer queue)**:
  - Events already consumed/acked by the server are **gone**.
  - Events published while the server is down are only visible after restart if the server’s
    durable per-consumer queue still exists and the messages are still within TTL.
  - Events that can't be published to RabbitMQ are dropped, not buffered on the worker. Each
    publish tries every host of the broker URL once (2s connect timeout per address, 2s per read
    or write after that). A single failure, quick or timed out, loses only its own event; three
    failures in a row make the worker skip publishing for a cooldown that grows from 1s to 30s
    and resets on the next successful publish, so during an outage each worker process makes
    at most three bounded attempts (plus any its other threads already had in flight) and then
    one per cooldown. Host names are resolved as for Redis: waiting at most the 2s connect
    timeout, cached for 30s, refreshed in the background and right after a failed connect.
    TLS behavior is unchanged: as with py-amqp alone, `amqps://` checks the certificate's host
    name only when the `ssl` options set `server_hostname`.
  - During a RabbitMQ memory or disk alarm, the broker accepts connections but stops reading
    from publishers, so a publish is sent but never confirmed. The worker counts such events as
    "sent but unconfirmed" (RabbitMQ may still deliver them once the alarm clears); three in a
    row open the breaker, and once an unconfirmed retry follows it retries only every 30s. Each
    attempt leaves one blocked connection on the broker until the alarm clears, so expect up to
    about 5 connections per worker process in the first minute of an alarm and about 2 per
    minute after that, all released when the alarm ends. Each retry also logs `Received method (10, 60) during closing channel 0`
    from py-amqp's `amqp` logger (not stemtrace's): that's RabbitMQ's "connection blocked"
    notice arriving while the connection closes.
  - Logging is bounded: at most four warnings a minute however the broker behaves (failures,
    an outage starting, an outage ending, and a summary). Each reports the number of events
    dropped, and sent but unconfirmed, since the previous one, and losses are reported within
    about 1.5 minutes even when the broker flaps.
  - Limitation: a broker that still answers within the 2s timeouts, but slowly, doesn't open
    the breaker. Each publish opens its own connection, so it waits for several round trips
    (connect, handshake, channel, publish, close): a broker that takes about 1s per reply costs
    each event several seconds.
  - A failover broker URL (`amqp://h1//;amqp://h2//`) must list nodes of the same RabbitMQ
    cluster: workers keep publishing to whichever host last worked, so the server must be able to
    consume those events from any of them. Entries resolve as in kombu: a bare host (`;h2`)
    takes user, password, port, virtual host and TLS options from the first entry, while a
    URL-form entry (`;amqp://h2`) gets guest/guest unless it names credentials, and takes port
    and virtual host from the previous entry only when it omits them (`//` means `/`). A host
    that refuses the login ends that publish attempt without trying the later hosts, as in
    kombu.
- **Workers + Registry tabs**: stemtrace uses **Celery inspect** on demand to populate workers
  and registered tasks, so those pages work even if the server missed `worker_ready` events.
- **If you need durable history across restarts**: point stemtrace events at Redis even if your
  Celery broker is RabbitMQ (set `transport_url` in `stemtrace.init_worker(...)` and set
  `STEMTRACE_TRANSPORT_URL` for the server / embedded setup).

## 🐳 Docker

```bash
docker run -p 8000:8000 \
    -e STEMTRACE_BROKER_URL=redis://host.docker.internal:6379/0 \
    ghcr.io/iansokolskyi/stemtrace
```

RabbitMQ example:

```bash
docker run -p 8000:8000 \
    -e STEMTRACE_BROKER_URL=amqp://guest:guest@host.docker.internal:5672// \
    ghcr.io/iansokolskyi/stemtrace
```

RabbitMQ broker + Redis event transport (recommended if you want history across server restarts):

```bash
docker run -p 8000:8000 \
    -e STEMTRACE_BROKER_URL=amqp://guest:guest@host.docker.internal:5672// \
    -e STEMTRACE_TRANSPORT_URL=redis://host.docker.internal:6379/0 \
    ghcr.io/iansokolskyi/stemtrace
```

Show a task argument as the graph node name (here `kwargs["operator_type"]`):

```bash
docker run -p 8000:8000 \
    -e STEMTRACE_BROKER_URL=redis://host.docker.internal:6379/0 \
    -e STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS=operator_type \
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

For a local RabbitMQ setup, see [`docker-compose.rabbitmq.yml`](docker-compose.rabbitmq.yml).

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
    --transport-url redis://myredis:6379/0 \
    --host 0.0.0.0 \
    --port 8000 \
    --node-alias-from-arguments operator_type \
    --reload  # For development
```

#### Node Names from Task Arguments

By default, graph nodes are labeled with the task name. Generic tasks (e.g. one `run_operator`
task that does different things depending on its arguments) are easier to tell apart when the
graph shows an argument instead:

- `--node-alias-from-arguments operator_type` uses `kwargs["operator_type"]`
- `--node-alias-from-arguments 0` uses `args[0]` (a digit string is a positional index)

If the argument is missing, the task name is shown. This is a **server** setting: set it on
`stemtrace server` (flag or `STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS`, e.g. in Docker) or pass
`node_alias_from_arguments` to `stemtrace.init_app(...)`. Surrounding whitespace is ignored, and
`stemtrace server` prints `Node alias: <key>` at startup when it is set. Workers need
`capture_args=True` (the default) so the arguments are available. Passing it to
`stemtrace.init_worker(...)` has no effect and is deprecated.

#### Protecting the Server (Built-in Login Page)

```bash
stemtrace server \
    --broker-url redis://myredis:6379/0 \
    --login-username admin \
    --login-password secret \
    --login-secret change-me
```

Set `--login-secret` (or `STEMTRACE_LOGIN_SECRET`) to a long random value, and use the same
value on every replica. Without it, each process generates its own random secret, so sessions
only work within a single process and are lost on restart.

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

Tip: you can also set `transport_url` if you want stemtrace events stored separately from your Celery broker.

#### Configuration Options

```python
import stemtrace

# Returns the underlying StemtraceExtension if you need it (optional).
extension = stemtrace.init_app(
    app,
    broker_url="redis://localhost:6379/0",
    transport_url=None,          # Defaults to broker_url
    prefix="/stemtrace",        # Mount path AND event stream prefix (normalized)
    ttl=86400,                  # Event TTL in seconds
    max_nodes=10000,            # Max nodes in memory
    embedded_consumer=True,     # Run consumer in FastAPI process
    serve_ui=True,              # Serve React dashboard
    auth_dependency=None,       # Optional auth (see below)
    node_alias_from_arguments=None,  # e.g. "operator_type" or "0"; see Node Names from Task Arguments
)
```

#### With Custom Authentication

```python
from fastapi import Depends
import stemtrace
from your_app.auth import require_admin

stemtrace.init_app(app, broker_url="redis://localhost:6379/0", auth_dependency=Depends(require_admin))
```

#### With Built-in Login Page (Recommended for UI-first)

If you primarily use the UI, the easiest way to protect **UI + assets + API + WebSocket**
is the built-in form login (cookie session):

```python
import stemtrace

stemtrace.init_app(
    app,
    broker_url="redis://localhost:6379/0",
    login_username="admin",
    login_password="secret",
    login_secret="change-me",  # recommended for production
)
```

This serves a sign-in page at `/stemtrace/login` and protects the UI, API and WebSocket under
`/stemtrace`. The static JS/CSS bundle under `/stemtrace/assets/` is the public UI build and is
served without a session. So is `/stemtrace/api/health/live`, a liveness probe for load
balancers and container health checks: it always answers 200 while the process is serving,
with only `{"status": "ok"}`, or `{"status": "degraded"}` when the embedded consumer stopped
unexpectedly or can't read from the broker (the same rule as `/stemtrace/api/health`).

For any multi-process deployment (`uvicorn --workers N`, gunicorn, or several replicas behind
a load balancer), set `login_secret` (or the `STEMTRACE_LOGIN_SECRET` env var) to the same long
random value everywhere, e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
Without it, each process signs sessions with its own random secret, and users get logged out
at random. stemtrace logs a warning at startup in that case, and the login page and UI explain
the problem when a session signed by another process is rejected.

#### Built-in Auth Helpers (Basic / API key)

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

### Health Endpoint

`GET /stemtrace/api/health` (under your mount prefix when embedded) always returns HTTP 200 with:

| Field | Meaning |
|-------|---------|
| `status` | `"ok"`, or `"degraded"` when the event consumer died unexpectedly, its loop keeps failing, or `consumer_connected` is `false`. A consumer stopped on purpose, or whose event source finished normally (`memory://`), stays `"ok"` |
| `version` | Installed stemtrace version |
| `consumer_running` | Whether the event consumer thread is alive |
| `consumer_connected` | `true` if the last broker read succeeded, `false` while reads fail, `null` if unknown (starting up, or the transport doesn't report it) |
| `websocket_connections` | Open UI WebSocket connections |
| `node_count` | Tasks currently held in memory |

Because the status code stays 200, check `status` in the body if you want monitoring to alert on a degraded consumer.

What is detected depends on the transport:

- **Redis:** refused or reset connections, connect timeouts, repeated read timeouts, and consumer loop failures. On redis-py < 8 the default socket timeout is unbounded, so a half-open or hung broker connection may go unnoticed; add `?socket_timeout=…` or `?health_check_interval=…` to the broker URL if you need that.
- **RabbitMQ:** refused or dropped connections and channel errors while connecting or draining, and consumer loop failures. The consumer connection is read about once a second and a read that times out counts as connected, so a half-open connection, or a broker that is frozen but keeps the socket open, may go unnoticed. Messages the consumer can't decode (not JSON) are dropped with a warning and don't count as failures.

## 🗺️ Roadmap

### What's Working Now

- ✅ **Task flow graphs** — Visualize chains, groups, and chords as DAGs
- ✅ **Full lifecycle tracking** — PENDING → RECEIVED → STARTED → SUCCESS/FAILURE
- ✅ **Canvas awareness** — Automatic GROUP/CHORD node visualization
- ✅ **Arguments & results** — View inputs, outputs, and exceptions
- ✅ **Task output** — What each attempt printed and logged (opt-in)
- ✅ **Sensitive data scrubbing** — Passwords and API keys filtered automatically
- ✅ **Real-time updates** — WebSocket-powered live dashboard
- ✅ **FastAPI integration** — Mount into your existing app
- ✅ **RabbitMQ support** — Use your existing RabbitMQ broker (`amqp://`, `amqps://`, `pyamqp://`)
- ✅ **Workers page** — Monitor online/offline workers and their registered tasks
- ✅ **Task registry** — Browse discovered + registered tasks with clear status badges

### Coming Soon

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
