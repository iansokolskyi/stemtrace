<img src="https://raw.githubusercontent.com/iansokolskyi/stemtrace/main/docs/brand/stemtrace-mark.svg" width="48" alt="stemtrace logo" />

# stemtrace

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

Stemtrace captures your task executions as a graph: visualize parent→child flows, see retry chains, track groups and chords, all without adding any new infrastructure.

It records each task's lifecycle (sent, started, retried, finished) with its arguments, result
and exception, and sends these events through the Redis or RabbitMQ broker you already run. The
dashboard runs as its own server or inside your FastAPI app. No database is needed.

<p align="center">
  <img src="https://raw.githubusercontent.com/iansokolskyi/stemtrace/main/docs/screenshots/workflow.png" width="900" alt="Chord visualization in the workflow graph" />
</p>

## What it helps you debug

| Question | Where stemtrace answers it |
|----------|----------------------------|
| What called this task? | The flow graph links each task to the task that started it, and draws groups and chords as containers. |
| Why did it retry, and which attempt worked? | The task page lists every attempt, with the reason for each retry and the exception and traceback of a failure. |
| Was it slow to start, or slow to run? | The task page shows how long the task spent in each state. |
| Why didn't my chord callback run? | The chord shows each header task's state. A chord whose body never ran (e.g. a header failed) shows no callback. |
| Why is this task stuck in PENDING? | The registry flags tasks that no current worker has registered. |
| What did this attempt print or log? | With `capture_output` turned on (opt-in), each attempt's output is on its task page. |

The [features page](https://github.com/iansokolskyi/stemtrace/blob/main/docs/features.md) shows
each dashboard view with screenshots.

## Is it ready for production?

What it adds to your system:

- **On workers:** events are published from Celery's task signals. Events that can't be
  published are dropped, not buffered. Every network step of a publish has a 2s timeout, and
  three failures in a row make the worker skip publishing for a cooldown that grows from 1s to
  30s. So during an outage each worker process makes at most three bounded attempts and then one
  per cooldown. Logging is bounded: at most four warnings a minute. On RabbitMQ, an event costs
  about 0.3 ms on the process's connection.
  [Brokers](https://github.com/iansokolskyi/stemtrace/blob/main/docs/brokers.md) has the
  details and limitations for each broker.
- **On your broker:** one Redis stream, or a RabbitMQ fanout exchange with a queue per server
  host, named with the `stemtrace` prefix by default.
- **The dashboard** is read-only: it never modifies your task queue. Common secrets in task
  arguments (passwords, API keys, tokens) appear as `[Filtered]`. It has a built-in login page
  with sign-in throttling, or takes your own FastAPI auth dependency, and a health endpoint
  for your monitoring.
  See [Security](https://github.com/iansokolskyi/stemtrace/blob/main/docs/security.md).

What to know before you rely on it:

- **History is kept in memory**, up to 10,000 tasks by default. A server restart starts empty:
  with Redis it replays the events still in the stream; with RabbitMQ, events it already
  consumed are gone. See
  [Event retention](https://github.com/iansokolskyi/stemtrace/blob/main/docs/retention.md).
- **The server is a single process** with the event consumer inside it. A multi-process mode is
  planned.
- **Task output is not scrubbed.** That's why capturing it is opt-in.
- **Limitations are documented next to each feature**, for example graph edge cases with
  `task.replace()` and older Celery versions in
  [Graphs](https://github.com/iansokolskyi/stemtrace/blob/main/docs/graphs.md).
- **stemtrace is pre-1.0.** Read the version's notes in the
  [CHANGELOG](https://github.com/iansokolskyi/stemtrace/blob/main/CHANGELOG.md) before you
  upgrade.

## stemtrace, Flower, or another tool?

Use stemtrace to understand how a workflow ran: which task started which, why a task retried,
and where a chord stopped.

Other tools fit better when you want to:

- **Watch workers live and act on them** (revoke tasks, change pool sizes or rate limits): use
  [Flower](https://flower.readthedocs.io/). stemtrace is read-only. The two can run side by side;
  see [Running next to Flower](https://github.com/iansokolskyi/stemtrace/blob/main/docs/adopting.md#running-next-to-flower).
- **Alert on queue length, throughput or failure rates:** use a metrics exporter for Celery
  and your existing alerting. stemtrace doesn't alert; its health endpoint only reports on
  stemtrace's own event consumer.
- **Trace a request across services**, from an HTTP call to the tasks it queued: use
  OpenTelemetry's Celery instrumentation with your tracing backend.
- **Keep task history for weeks or months:** stemtrace keeps recent tasks in memory.

## Quick start

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

Access at `/stemtrace/` in your existing app, with no new services to deploy.

Adding stemtrace to a project that's already in production? Read
[Adopting stemtrace in an existing project](https://github.com/iansokolskyi/stemtrace/blob/main/docs/adopting.md)
first: it covers which processes need the package, version requirements, and what changes on
your broker. See [Deployment](https://github.com/iansokolskyi/stemtrace/blob/main/docs/deployment.md)
for Docker, auth, reverse proxies and health checks.

## Documentation

| Page | What's in it |
|------|--------------|
| [Adopting stemtrace](https://github.com/iansokolskyi/stemtrace/blob/main/docs/adopting.md) | What changes in workers, senders and the broker; running next to Flower |
| [Features](https://github.com/iansokolskyi/stemtrace/blob/main/docs/features.md) | What the dashboard shows, with screenshots; workers and registry |
| [Graphs](https://github.com/iansokolskyi/stemtrace/blob/main/docs/graphs.md) | How chains, groups, chords and `task.replace()` are drawn, and the limitations |
| [Configuration](https://github.com/iansokolskyi/stemtrace/blob/main/docs/configuration.md) | Worker and server options, task output, RECEIVED events, environment variables |
| [Deployment](https://github.com/iansokolskyi/stemtrace/blob/main/docs/deployment.md) | Standalone server, Docker, FastAPI embedding, reverse proxies, health endpoint |
| [Security](https://github.com/iansokolskyi/stemtrace/blob/main/docs/security.md) | Sensitive data scrubbing, built-in login and its throttling, custom auth |
| [Brokers](https://github.com/iansokolskyi/stemtrace/blob/main/docs/brokers.md) | How workers publish to Redis and RabbitMQ, outages, limitations |
| [Event retention](https://github.com/iansokolskyi/stemtrace/blob/main/docs/retention.md) | What survives a server restart, durable history |
| [Architecture](https://github.com/iansokolskyi/stemtrace/blob/main/docs/architecture.md) | How the library and the server fit together |

Planned work and ideas are tracked in [GitHub issues](https://github.com/iansokolskyi/stemtrace/issues). Open one, or comment on an existing one, to say what you'd use.

## Contributing

Contributions, bug reports, and feature requests are welcome! This is a community project: if stemtrace helps you debug Celery, consider helping make it better.

See our [Contributing Guide](https://github.com/iansokolskyi/stemtrace/blob/main/CONTRIBUTING.md) to get started.

```bash
git clone https://github.com/iansokolskyi/stemtrace.git
cd stemtrace
uv sync --extra dev   # Install dependencies
make check            # Run tests
```

## License

MIT: use it however you like.
