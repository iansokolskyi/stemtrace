<img src="https://raw.githubusercontent.com/iansokolskyi/stemtrace/main/docs/brand/stemtrace-mark.svg" width="48" alt="stemtrace logo" />

# stemtrace 🌿

**Zero-infrastructure Celery task flow visualizer**

[![PyPI version](https://img.shields.io/badge/pypi-v0.4.0-darklime)](https://pypi.org/project/stemtrace)
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

    # RECEIVED events (disabled by default, see "Received Events" below)
    publish_received=False,                    # Publish RECEIVED when a worker takes a message
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

Keys match by substring, whatever the key's case (`USER_PASSWORD` matches
`password`); give `additional_sensitive_keys` in lowercase. Bytes keys (such as
redis-py's `hgetall()` without `decode_responses`) are decoded as UTF-8 and
matched the same way. A tuple or frozenset key matches if any of its parts
does, and an Enum member by its name or its value: that includes `IntEnum` and
`StrEnum` members, so `HTTPStatus.UNAUTHORIZED` is filtered like any key
containing `auth`. Other keys (ints, `None`, other objects) never match, but
dicts inside their values are still scrubbed.
Task data stemtrace can't capture, such as a value whose `str()` raises or a
cyclic structure, appears as `[Not captured: <error type>]`.

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

#### Received Events (opt-in)

By default a task's events are PENDING (sent), STARTED and SUCCESS/FAILURE. Turn on
`publish_received` on the worker to also publish a RECEIVED event when the worker takes the
task message, so the UI shows when a task reached a worker before it started, e.g. while it
waits for its ETA or for a free pool slot:

```python
stemtrace.init_worker(app, publish_received=True)
```

It is off by default because it adds one synchronous publish per task message to the worker's
consumer loop. Under prefork that loop is the worker's main process, so every task waits for it:
on RabbitMQ it costs about 0.7 ms per message, about 22% of the throughput of no-op tasks
(measured with prefork and RabbitMQ connection reuse). Redis was not measured.

- It is published for each delivery of a task's first attempt. A retry's message adds none (its
  RETRY event covers it). A message redelivered after a lost worker (`acks_late` with
  `reject_on_worker_lost`) adds a second one, and both are listed ahead of the task's STARTED
  events: the main process publishes RECEIVED and the pool child publishes STARTED, with clocks
  that can differ, so first-attempt RECEIVED events are always listed first.
- Verified with real workers on the prefork, threads, solo, gevent and eventlet pools.

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

# Group of chains: each chain is a row inside the group
┌─ GROUP ─────────────────────────┐
│  fetch(1) ──► parse(1)          │
│  fetch(2) ──► parse(2)          │
└─────────────────────────────────┘
```

- **Synthetic containers** — GROUP/CHORD nodes are always created when 2+ tasks share a `group_id`
- **Parent linking** — When spawned from a parent task, the container becomes a child of that parent
- **Chord callbacks** — Rendered outside the container with edges from each header task (verified with Celery 5.6; on Celery 5.2 a chord whose body is a chain, or `chain(group(...), task)`, carries no chord callback ID and shows as a group, with the chain hanging off the header that finished last, inside the group). The callback is the task the chord's body starts with (for a chain body, the rest of the chain follows it). It is only drawn on evidence from the events: the callback the headers name, or the chord's group ID that Celery stamps on a chain body's last task, traced back to a header (or Celery's `chord_unlock` task). A chord whose body never ran (e.g. a header failed) shows no callback, and a header's own subtasks are never mistaken for it
- **Replaced tasks** — `task.replace()` makes Celery reuse the replaced task's ID for the replacement's last task, so one graph node stands for both. It stays where the original task was sent: its name and parent come from its own events, not the replacement's (which report a task below it as parent, its child or, for a longer chain, a task further down; or, when the replacement is a single task, the task itself), whatever the hosts' clocks say; the replacement's other tasks hang from it, and a callback that reuses the ID can't also be drawn after a chord. The same holds when one of the replacement's tasks is replaced in turn (a nested `replace()`, up to 16 levels deep), and for a task that keeps replacing itself (recursive pagination: every page's tasks hang from one node). A server that starts after the replaced task ran only sees the replacement's events: the node shows the replacement's name. A replaced workflow root stays the root, with the replacement's other tasks under it. A replaced task inside a workflow loses its link to its parent and forms a graph of its own with the replacement's tasks. After a two-task replacement, it and the replacement's first task name each other as parent, and the one with the larger task ID is on top; after a longer replacement chain they name each other in a loop, and which of them is on top depends on the order events arrive in
- **Chains in groups** — For `group(chain(a1, b1), chain(a2, b2))` Celery only marks `b1` and `b2` as group members. A member's parent task whose only child is that member, and which belongs to no group and isn't a chord callback, is drawn inside the group as part of the member's chain (and so on up the chain), so the whole workflow is one graph. A parent with other children (e.g. one that started the group, or spawned other tasks) is never pulled in. Chains are drawn once every member has reported its parent. A chord whose body is a group (`chord([a, b], group(c, d))`) draws that group after the chord
- **Limitation: shared chord tasks** — Celery workflows don't produce events in which two chords share one `celery.chord_unlock` task or one task that starts a group body. If such events arrive anyway (hand-crafted or malformed), the graph stays consistent, but which chord draws the shared task, and whether a chord's body shows as empty, can depend on the order the events arrive in.
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
| `STEMTRACE_ROOT_PATH` | Server only: URL prefix stripped by a reverse proxy in front of `stemtrace server` (see [Server Options](#server-options)). Same as `--root-path`. | Unset. |
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
  - Workers resolve the broker's host name in a background thread and wait at most the 2s
    connect timeout for it, so an unreachable DNS resolver costs an attempt about 2s too (it
    would otherwise block for the resolver's own timeouts, measured at 20s). Answers are cached
    per process for 30s and refreshed in the background, keeping the last answer while DNS
    fails; a failed lookup with no earlier answer is retried after 5s. So a process looks a
    host up when it first publishes, then about once per 30s while it keeps publishing, after
    a failed connect, and at most every 5s while lookups fail. When connecting to every cached
    address fails, the answer is refreshed right away, so after a DNS failover the next attempt
    (at the latest the first retry after the breaker's cooldown) uses the new address; a host
    that moved while its old address still accepts connections is picked up within the 30s
    cache time. TLS behavior is unchanged.
  - A process that forks while a lookup thread is inside glibc's DNS code can hand the child
    a lock that deadlocks it. So the forking process (the Celery prefork master, not the pool
    children or web workers) waits up to 1s for its lookups before each fork, and after its
    first fork it resolves in a short-lived helper process instead (the same Python, so the
    same `/etc/hosts`, `resolv.conf` and name services), which never holds such a lock. Each
    such lookup starts a Python process: about 60-80ms in a `python:3.13-slim` container on an
    unloaded host, more on a small CPU share. Where the helper can't run (a frozen app), the
    forking process keeps resolving in-process and
    every fork waits up to 1s while a lookup that started less than 5s ago (glibc's per-try
    timeout), or that no fork waited for yet, is in flight; stemtrace logs a warning once. A
    helper is never started while the process forks, so a child can't inherit its pipes.
  - Under uWSGI (or pyuwsgi), stemtrace resolves on the calling thread, without the 2s bound,
    in every uWSGI process (master or not, workers, mules, spoolers, lazy apps), as before
    0.3.11: with a dead DNS resolver a publish waits for the resolver's own timeouts (20s
    measured). uWSGI forks in C without running Python's fork hooks (unless started with
    `--py-call-uwsgi-fork-hooks`, which stemtrace doesn't act on), and any of its processes
    may fork (the master, the first process when there is no `--master`, or app code calling
    `os.fork()`), so a fork can't wait for lookup threads. These processes therefore start no
    lookup thread or helper process at all, and concurrent threads resolving the same host each
    run their own lookup. After a failed connect, the next publish
    re-resolves on its own thread rather than in the background. A background thread of the
    app that publishes can still be inside glibc's DNS code during a fork: the same exposure
    as that thread's own broker lookups (pyuwsgi 2.0.30 runs threads without
    `--enable-threads`). On Windows (no fork) lookups always run in-process.
  - Once a forking process uses the helper, it resolves with the system's resolver in a new
    Python process, so a monkeypatched `socket.getaddrinfo` (a custom resolver shim, or
    gevent's c-ares resolver) isn't used for its lookups. Under gevent, `subprocess` starts
    programs with a Python-level fork, so any `subprocess` call in a gevent worker counts as a
    fork and switches that worker to the helper. Under eventlet, the green `subprocess` forks
    in C without Python's fork hooks, so the worker isn't switched to the helper and nothing
    waits for its lookups before such a fork. The same applies to RabbitMQ.
  - A fork waits for any helper that is being started to finish its exec first, with no time
    limit (the helper's pipes would otherwise end up in a child that never execs). If that
    exec stalls (e.g. a hung file system), forks stall with it.
  - A Redis that still answers within 2s doesn't open the breaker, however slow: each publish
    waits for its reply.
  - Logging is bounded as for RabbitMQ below: at most four warnings a minute, each with the
    number of events dropped since the previous one.
  - Limitation: any `socket_timeout` under 0.5s in the broker URL (the server's consumer reads
    with half of it as the XREAD block window) adds up to about 0.25s of latency to a new event:
    an idle read that returns sooner than that (`MIN_IDLE_READ_SECONDS`) is followed by a wait
    for the rest, instead of looping at once. With redis-py's default timeouts nothing changes.
  - Limitation: a Celery soft time limit that lands while a task signal (`task_prerun`,
    `task_postrun`, ...) is publishing is not delivered to the task. stemtrace lets the
    exception propagate and doesn't count it as a Redis failure, but Celery's `Signal.send`
    catches what a receiver raises, so the task keeps running and Celery logs an ERROR,
    `Signal handler ... raised: SoftTimeLimitExceeded()`. A limit that lands while the task
    itself prints or logs with `capture_output` on is delivered.
- **RabbitMQ (fanout + per-consumer queue)**:
  - Events already consumed/acked by the server are **gone**.
  - Events published while the server is down are only visible after restart if the server’s
    durable per-consumer queue still exists and the messages are still within TTL.
  - Each worker process publishes on one RabbitMQ connection that it opens with its first event and
    keeps: an event costs about 0.3 ms instead of about 9 ms for a connection of its own. Prefork
    children open their own after the fork and never use the master's (Python's fork hooks, plus a
    process-ID check for forks that skip them, such as uWSGI's). Threads, gevent and eventlet pools
    share their process's connection and publish one event at a time (gevent and eventlet need
    `threading` patched, as Celery's pools do). RabbitMQ lists these connections as
    `stemtrace publisher <host> pid <pid>`. The connection negotiates no AMQP heartbeat, also when
    the broker URL says `?heartbeat=N`: nothing runs between events to send one. It is closed when
    the interpreter exits and when a prefork child exits (waiting at most half a second for the
    broker's reply); a process killed or ended with `os._exit()` leaves it to the operating system,
    and RabbitMQ logs a warning about the dropped TCP connection.
  - The server's consumer connection does use `?heartbeat=N` from the broker URL: its read loop
    sends the heartbeats and waits at most a quarter of the interval for frames (at most 1s), so
    an idle consumer is not dropped by the broker, and a broker that stops sending heartbeats is
    treated like a dropped connection (reconnect with backoff), also when the broker is frozen with
    its TCP connection up (`docker pause`): a failed consumer connection is dropped without the
    AMQP close handshake, which would wait forever for a frozen broker.
  - Events that can't be published to RabbitMQ are dropped, not buffered on the worker. Events
    publish one at a time on the process's connection, and **an event never waits more than 2s
    in total (one IO timeout) for another to finish: it is dropped instead**, which doesn't
    count as a failure. The event that has to connect takes longer. It tries every host of the
    broker URL once, and each step waits at most 2s: resolving the name, the TCP connect (2s in
    all for the addresses of a name: a refused address costs microseconds and never excludes a
    live one, and after a black-holed one the next attempt starts with the next address), the TLS
    handshake, each of the three AMQP handshake replies and, after a failure over TLS, the
    closing exchange; then opening the channel, declaring the exchange and writing the event.
    That is at most about 20s with one URL host, and 14s more for each further host, though a
    healthy broker takes milliseconds. A slow broker whose every step stays within 2s keeps
    publishing and doesn't open the breaker (events that arrive while a slow connect is in
    progress are dropped). If the thread that took the connection never gives it back (a signal
    handler raised at the wrong moment), a thread that finds the lock held by itself starts over
    with a new connection at once; an event from another thread does after its wait (2s) if the
    holder never recorded the lock, and otherwise only once the holder has had the longest an
    attempt can take (about 20s with one host): until then events that find it taken are dropped
    after their wait. Starting over logs the warning `RabbitMQ publisher connection was not
    released by its last user; starting over with a new connection`. A single failure, quick or
    timed out, loses only its own event and drops the connection; the next event connects again.
    Three failures in a row make the worker skip publishing for a cooldown that grows from 1s to
    30s and resets on the next successful publish, so during an outage each worker process makes
    at most three bounded attempts and then one per cooldown. Host names are resolved as for
    Redis: waiting at most the 2s connect timeout, cached for 30s, refreshed in the background and
    right after a failed connect. TLS behavior is unchanged: as with py-amqp alone, `amqps://`
    checks the certificate's host name only when the `ssl` options set `server_hostname`.
  - A connection that RabbitMQ closed while it was idle (a broker restart, `rabbitmqctl
    close_connection`, the management UI) is noticed before the next event, which goes out on a new
    connection; that doesn't count as a failure unless connecting fails. The connection is only
    read when it has something to say, and a frame that arrives in pieces (a frozen broker, or one
    that trickles in a byte at a time) costs the event at most about 0.1s in total (two reads of at
    most 50 ms) and is finished later, so it never spins or stalls the other greenlets. Limitations: an event
    written at the moment the broker closes the connection, or within about a second after the
    network drops without closing it (py-amqp 5.3 sets `TCP_USER_TIMEOUT` to 1s), can be lost
    without an error; and after the exchange is deleted, the next event is lost (RabbitMQ closes the
    channel because of it) and the one after it re-declares the exchange on a new connection.
  - During a RabbitMQ memory or disk alarm, RabbitMQ blocks a publisher's connection once the first
    event published during the alarm arrives, and sends Connection.Blocked. The worker reads that
    before its next event and drops events without sending them until RabbitMQ sends
    Connection.Unblocked; three drops in a row open the breaker, and its retries only read the
    connection, so an alarm adds no connections and no "sent but unconfirmed" events (RabbitMQ
    delivers the event that triggered the block when the alarm ends).
  - Logging is bounded: at most four warnings a minute however the broker behaves (failures,
    an outage starting, an outage ending, and a summary). Each reports the number of events
    dropped, and sent but unconfirmed, since the previous one, and losses are reported within
    about 1.5 minutes even when the broker flaps.
  - Limitation: a broker that still answers within the 2s timeouts, but slowly, doesn't open the
    breaker. An event on an open connection only waits for a write, but each new connection waits
    for several round trips (connect, handshake, channel, exchange declare): a broker that takes
    about 1s per reply costs the event that has to connect several seconds. While the broker is
    paused or unreachable without the connection closing (`docker pause`), written events are
    buffered by the operating system and delivered when it answers again; the worker sees a failure
    only once the buffers are full or the connection drops.
  - Limitation: a Celery soft time limit that lands while a task signal (`task_prerun`,
    `task_postrun`, ...) is publishing is not delivered to the task. stemtrace lets the
    exception propagate and doesn't count it as a RabbitMQ failure, but Celery's `Signal.send`
    catches what a receiver raises, so the task keeps running and Celery logs an ERROR,
    `Signal handler ... raised: SoftTimeLimitExceeded()`. A limit that lands while the task
    itself prints or logs with `capture_output` on is delivered.
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
  - A Celery nodename (`name@host`) has at most one live process, so a worker is shown online
    only for its newest pid: when a node restarts, its older pids go offline, including one that
    was killed without a `worker_shutdown` event. A ping reply carries no pid, so it can only
    bring back the newest pid, never one that sent `worker_shutdown`. When the stream and an
    inspect reply disagree, whichever is newer wins: events that arrive while the server waits
    for a reply beat that reply.
  - **Limitation: duplicate nodenames.** Two live workers that share a nodename (e.g. started
    with the same `-n`, which Celery itself warns about with `DuplicateNodenameWarning`) can't be
    told apart: Celery keeps only the last reply per nodename, so they flip between online and
    offline on every refresh. Give each worker a unique nodename.
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
    --root-path /monitoring \
    --reload  # For development
```

`--root-path` (or `STEMTRACE_ROOT_PATH`) is for a reverse proxy that serves stemtrace under a
path prefix and strips it before forwarding (e.g. `https://example.com/monitoring/` →
`http://stemtrace:8000/`). It is passed to uvicorn as its `root_path`, so the UI, login
redirects and session cookies use the prefixed URLs. Leave it unset when the proxy forwards
the full path or serves stemtrace at the root of a host. The prefix may only contain
`/`-separated segments of letters, digits, `.`, `_` and `-` (up to 64 characters each);
`stemtrace server` refuses to start with anything else, and an embedded app whose `root_path`
breaks this rule logs a warning and serves UI links without the prefix.

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
only work within a single process and are lost on restart. Failed sign-ins are throttled per IP
and per username or browser; see [With Built-in Login Page](#with-built-in-login-page-recommended-for-ui-first)
for the limits and the proxy setting (`FORWARDED_ALLOW_IPS`, set to your proxies' addresses,
never `*`) that keeps client IPs correct.

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

#### FastAPI Telemetry

FastAPI 0.142 and later trace requests natively when an OpenTelemetry SDK is configured in your process. Embedded, stemtrace's routes are part of your app, so each request the dashboard makes to `/stemtrace/api/...` and each `/stemtrace/ws` connection (one span for as long as the browser stays connected) gets a span. To keep them out of your traces, exclude the prefix:

```python
app = FastAPI(
    lifespan=my_lifespan,
    telemetry={
        "exclude": lambda scope: scope.get("path", "") == "/stemtrace"
        or scope.get("path", "").startswith("/stemtrace/")
    },
)
```

Use your own `prefix` if you changed it. `telemetry={"tracing": False}` turns tracing off for the whole app.

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

Failed sign-ins are throttled in memory, per client IP and per username or browser:

- After 5 failures from one IP (IPv6: one /64 network), further attempts from it get
  `429 Too Many Requests` with a `Retry-After` header, without the credentials being checked.
  The first lockout is 30 seconds; each further failure doubles it, up to 15 minutes.
- A successful sign-in sets a *device cookie* (`stemtrace_session_device`, signed, kept on
  logout). A browser that sends a valid one for the username it signs in with gets its own
  limit of 5 failures and skips the IP limit (its failures still count against the IP). Every
  other browser shares a per-username limit of 20 failures, from all IPs together.
- Device cookies stay valid for 90 days only with a configured `STEMTRACE_LOGIN_SECRET`
  (`login_secret` / `--login-secret`). With a generated per-process secret they stop working
  on every restart and aren't recognized by other workers or replicas.
- The per-username limit can be held by anyone who knows the username: about one failed
  attempt every 15 minutes keeps sign-in locked, **indefinitely**, for every browser without a
  valid device cookie, e.g. a new laptop or a cleared browser. Browsers that signed in within
  the last 90 days are not affected. Restarting stemtrace clears all limits. If that's a
  concern, put stemtrace behind your own auth (see below) or a proxy with its own rate limits.
- Guessing costs the same for every username, real or not: 20 guesses, then one per lockout
  (about 4 an hour at the 15-minute cap), or, waiting out the hour after which failures are
  forgotten, another burst of 20. So the sustained maximum is about 20 guesses an hour per
  username. A count goes down only after an hour without failures or after a successful
  sign-in, which clears the counts for its IP, username and device. The response is the same
  whether the username exists or not.
- The client IP is the one your ASGI server reports; `X-Forwarded-For` is never read directly.
  Behind a reverse proxy or load balancer, tell the server which proxies to trust with uvicorn
  `--forwarded-allow-ips` or the `FORWARDED_ALLOW_IPS` env var (which `stemtrace server` also
  reads), listing their IPs or CIDRs, e.g. `FORWARDED_ALLOW_IPS=10.0.0.0/8`. Don't use `*` with
  a proxy that appends to `X-Forwarded-For`: uvicorn then takes the leftmost entry, which the
  client sets, so anyone can claim a new IP on every request. Without the setting, every user
  shares the proxy's address and its IP limit (browsers with a device cookie aren't held up by
  that). Requests without a client address (e.g. uvicorn `--uds`) have no IP limit; the
  username and device limits still apply.
- Limits are per process: with several workers or replicas, each counts on its own.
- Failures are counted in fixed tables of 65,536 buckets per kind (IP, username, device;
  about 1.5 MB each), not per key, so memory never grows and failure traffic can't push out or
  lower another key's count. Each key maps to two buckets through a hash keyed with a random
  per-process secret, and is locked only if both are. Keys that share buckets can therefore
  only be locked early, never late. (A successful sign-in clears its own two buckets, so with a
  probability of about 0.006% per sign-in it can also lift the lock of a key sharing both.)
  False locks for a username that never failed: about 0.02% with 500 usernames locked at
  once, 0.08% with 1,000, 2% with 5,000 and 21% with 20,000 (an attacker locking junk
  usernames on purpose); IPs behave the same. Junk-IP floods are cheap for anyone with many
  addresses (an IP locks after 5 failures, so 5,000 IPs take 25,000 requests) and lock out
  that share of other IPs, for browsers without a device cookie. Browsers with a device cookie
  are never affected by IP or username buckets.

Behind a reverse proxy that strips a path prefix, run your app with uvicorn's `--root-path`
(e.g. `uvicorn app:app --root-path /monitoring`); the login page, redirects and the session
cookie then use `/monitoring/stemtrace/...`.

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
- ✅ **Full lifecycle tracking** — PENDING → STARTED → SUCCESS/FAILURE, with RECEIVED when `publish_received=True` is set on the worker
- ✅ **Canvas awareness** — Automatic GROUP/CHORD node visualization
- ✅ **Arguments & results** — View inputs, outputs, and exceptions
- ✅ **Task output** — What each attempt printed and logged (opt-in)
- ✅ **Sensitive data scrubbing** — Passwords and API keys filtered automatically
- ✅ **Real-time updates** — WebSocket-powered live dashboard
- ✅ **FastAPI integration** — Mount into your existing app
- ✅ **RabbitMQ support** — Use your existing RabbitMQ broker (`amqp://`, `amqps://`, `pyamqp://`)
- ✅ **Workers page** — Monitor online/offline workers and their registered tasks
- ✅ **Task registry** — Browse discovered + registered tasks with clear status badges

### What's Next

Planned work and ideas are tracked in [GitHub issues](https://github.com/iansokolskyi/stemtrace/issues). Open one, or comment on an existing one, to say what you'd use.

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
