# Deployment

`stemtrace` offers two deployment modes depending on your needs:

| Mode | Best For | Command |
|------|----------|---------|
| **Standalone Server** | Dedicated monitoring, simple setup | `stemtrace server` |
| **FastAPI Embedded** | Single-app deployment, existing FastAPI apps | `stemtrace.init_app(...)` |

Either way, your workers need `stemtrace.init_worker(app)`; see
[Adopting stemtrace](adopting.md).

- [Standalone server](#standalone-server)
- [Docker](#docker)
- [Embedded in FastAPI](#embedded-in-fastapi)
- [Behind a reverse proxy with a path prefix](#behind-a-reverse-proxy-with-a-path-prefix)
- [Running several processes](#running-several-processes)
- [Health endpoint](#health-endpoint)

## Standalone server

(Recommended.) The standalone server runs stemtrace as a dedicated monitoring service:

```bash
pip install stemtrace

stemtrace server
```

Open [http://localhost:8000](http://localhost:8000) to view the dashboard.

All flags are listed in [Server options](configuration.md#server-options). To require a sign-in,
see [Protecting the standalone server](security.md#protecting-the-standalone-server).

## Docker

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

For a local RabbitMQ setup, see [`docker-compose.rabbitmq.yml`](../docker-compose.rabbitmq.yml).

## Embedded in FastAPI

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

All `init_app()` arguments are listed in
[Embedded server options](configuration.md#embedded-server-options-init_app). For
authentication, see [Protecting an embedded dashboard](security.md#protecting-an-embedded-dashboard).

### FastAPI telemetry

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

## Behind a reverse proxy with a path prefix

`--root-path` (or `STEMTRACE_ROOT_PATH`) is for a reverse proxy that serves stemtrace under a
path prefix and strips it before forwarding (e.g. `https://example.com/monitoring/` →
`http://stemtrace:8000/`). It is passed to uvicorn as its `root_path`, so the UI, login
redirects and session cookies use the prefixed URLs. Leave it unset when the proxy forwards
the full path or serves stemtrace at the root of a host. The prefix may only contain
`/`-separated segments of letters, digits, `.`, `_` and `-` (up to 64 characters each);
`stemtrace server` refuses to start with anything else, and an embedded app whose `root_path`
breaks this rule logs a warning and serves UI links without the prefix.

For an embedded dashboard, run your app with uvicorn's `--root-path`; see the end of
[Built-in login page](security.md#built-in-login-page). Behind any proxy, the built-in login
needs `FORWARDED_ALLOW_IPS` to see client IPs; see the same section.

## Running several processes

Note: `stemtrace server` includes an embedded consumer today (single-process). A multi-process deployment mode is planned.

The store lives in the memory of the process that runs the consumer. Every `stemtrace server`
process, and with the default `embedded_consumer=True` every process of an app that calls
`init_app`, starts its own consumer and keeps its own store. Behind a load balancer, requests
answered by different processes can therefore show different data. On RabbitMQ the processes
on one host also share one queue (`<prefix>.events.<hostname>`), so each of them receives only
some of the events. Serve the dashboard from one process: one `stemtrace server`, or an app run
with a single worker process.

Embedded consumer modes:

| Mode | Use Case | Setup |
|------|----------|-------|
| Embedded | Development, simple apps | Default: consumer runs in FastAPI process |
| External | Production, high scale | Planned |

If you run several processes or replicas anyway, accepting that they can show different data,
and use the built-in login, set the same `STEMTRACE_LOGIN_SECRET` everywhere; see
[Built-in login page](security.md#built-in-login-page).

## Health endpoint

`GET /stemtrace/api/health` (under your mount prefix when embedded) always returns HTTP 200 with:

| Field | Meaning |
|-------|---------|
| `status` | `"ok"`, or `"degraded"` when the event consumer died unexpectedly, its loop keeps failing, or `consumer_connected` is `false`. A consumer stopped on purpose, or whose event source finished normally (`memory://`), stays `"ok"` |
| `version` | Installed stemtrace version |
| `consumer_running` | Whether the event consumer thread is alive |
| `consumer_connected` | `true` if the last broker read succeeded, `false` while reads fail, `null` if unknown (starting up, or the transport doesn't report it) |
| `websocket_connections` | Open UI WebSocket connections |
| `node_count` | Tasks currently held in memory |

Because the status code stays 200, check `status` in the body if you want monitoring to alert on a degraded consumer. With an [API key](security.md#api-key-for-scripts-and-coding-agents), this endpoint needs it: send `Authorization: Bearer <key>`. With only the [login page](security.md#built-in-login-page), it needs a session cookie. For checks without credentials, probe `/stemtrace/api/health/live`, which needs neither (a custom `auth_dependency` still applies to it).

What is detected depends on the transport:

- **Redis:** refused or reset connections, connect timeouts, repeated read timeouts, and consumer loop failures. On redis-py < 8 the default socket timeout is unbounded, so a half-open or hung broker connection may go unnoticed; add `?socket_timeout=…` or `?health_check_interval=…` to the broker URL if you need that.
- **RabbitMQ:** refused or dropped connections and channel errors while connecting or draining, and consumer loop failures. The consumer connection is read about once a second and a read that times out counts as connected, so a half-open connection, or a broker that is frozen but keeps the socket open, may go unnoticed. Messages the consumer can't decode (not JSON) are dropped with a warning and don't count as failures.

`/stemtrace/api/health/live` is a liveness probe that stays public with the built-in login; see
[Built-in login page](security.md#built-in-login-page).
