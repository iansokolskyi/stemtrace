# Adopting stemtrace in an existing project

This page is for a team that already runs Celery and wants to add stemtrace: what changes in
which process, what you see before and after, and how it sits next to Flower.

- [What changes where](#what-changes-where)
- [What the server shows before your workers are instrumented](#what-the-server-shows-before-your-workers-are-instrumented)
- [Running next to Flower](#running-next-to-flower)
- [Removing stemtrace](#removing-stemtrace)

## What changes where

### 1. Install the package where Celery code runs

Install `stemtrace` in every environment that runs your Celery app: the workers, and every
process that sends tasks (a web app, a script, anything that calls `.delay()`,
`.apply_async()` or `app.send_task()`).

The package has one set of dependencies for both sides, so a web app that only needs the
worker instrumentation still installs the server's dependencies. Check these floors against
your lock file before you start:

- Python 3.10 or later
- `celery>=5.2.2`, `kombu>=5.2.2`, `pydantic>=2.4.0` (Pydantic 1 is not supported)
- `redis>=4.1.0`, `fastapi>=0.112.1`, `starlette>=0.38.6`, `typer>=0.13.0`,
  `uvicorn[standard]>=0.20.0`, `httpx>=0.24.0`

### 2. Call `init_worker` in the module that defines your Celery app

```python
app = Celery("myapp", broker="redis://localhost:6379/0")
stemtrace.init_worker(app)
```

Putting it next to the app definition means every process that imports the app runs it, on
both sides:

- **Workers** publish STARTED, RETRY, SUCCESS/FAILURE and REVOKED events from Celery's task
  signals (and RECEIVED, if you turn on
  [`publish_received`](configuration.md#received-events-opt-in)), plus an event when a worker
  starts and when it shuts down.
- **Senders** publish the PENDING event when a task message is published (Celery's
  `after_task_publish` signal). It carries the parent, root and group IDs, so a task is placed
  in its graph as soon as it is sent. A process that sends tasks without `init_worker` sends no
  PENDING event: its tasks appear only once a worker reports them, so their time in the queue
  isn't shown and a task that no worker has taken isn't listed. Tasks sent from inside other
  tasks are sent by the worker, so they are covered. Eager `apply()` publishes no message and
  so no PENDING event.

stemtrace doesn't use Celery's own event stream, so you don't need to start workers with `-E`.

### 3. Know what it writes to your broker

By default events go to the broker in Celery's `broker_url` (set `transport_url` to send them
elsewhere; see [Worker options](configuration.md#worker-options)):

- **Redis**: one stream, `<prefix>:events` (`stemtrace:events` by default), trimmed to the last
  `max(ttl, 10000)` events. With [task output](configuration.md#task-output-print-and-logging)
  on, those events can be large; that section has the numbers.
- **RabbitMQ**: a durable fanout exchange, `<prefix>.events`, and one durable queue per server
  host, `<prefix>.events.<hostname>`, whose messages expire after the server's `ttl`.

Publishing is bounded by timeouts and a circuit breaker, and events that can't be published are
dropped rather than buffered; [Brokers](brokers.md) has the details for each broker.

### 4. Run the server

Run `stemtrace server` (or mount it with `init_app`) against the same broker; see
[Deployment](deployment.md). The dashboard keeps its state in one process, so if you mount it
in a web app that runs several worker processes, read
[Running several processes](deployment.md#running-several-processes) first. `stemtrace server` reads events under the default prefix,
`stemtrace`. If you pass another `prefix` to `init_worker`, mount the dashboard with
`init_app(..., prefix=...)` set to the same value: there it is both the mount path and the event
prefix.

### 5. Redeploy

`init_worker` runs when your app module is imported, so running workers and web processes pick
it up only when they restart. stemtrace shows only events published after that: there is no
backfill of tasks that ran before.

When you upgrade stemtrace later, read the **Upgrading** note in the
[CHANGELOG](../CHANGELOG.md) for that version. For 0.4.0 it says to upgrade the server before
(or together with) the workers.

## What the server shows before your workers are instrumented

You can start the server first, before any worker runs `init_worker`:

- **Workers** and **Registry** pages: when the UI requests their data, the server asks the
  workers on its broker URL with Celery inspect, at most once every 2 seconds per server process.
  It sends `stats` and `registered` one after the other (`ping` too when `stats` gets no reply),
  each waiting until 0.5 s pass without a reply, and the page waits for them: at least 1 s,
  about 18 s when the broker refuses connections or its name doesn't resolve, and several
  minutes when connecting times out (e.g. a firewalled host). Every
  worker that answers is listed, instrumented or not, with the tasks it has registered. Those
  tasks show **Never Run** until stemtrace receives an event for one of them.
- **Tasks** and **graphs** stay empty until instrumented processes publish events.
- The [health endpoint](deployment.md#health-endpoint) tells you whether the server's consumer
  can read from the broker.

## Running next to Flower

You can keep Flower while you try stemtrace; they read different data:

- Flower reads Celery's own event stream, which workers send when started with `-E` (or with
  `worker_send_task_events` set). stemtrace doesn't read or change that setting: it publishes
  its own events, from Celery's signals, to the stream or exchange described
  [above](#3-know-what-it-writes-to-your-broker). It works the same whether Celery events are on or off.
- Both ask workers for their state with Celery inspect. stemtrace only sends the `stats`,
  `registered` and `ping` requests described above, whenever Workers or Registry data is
  requested (page loads, registry search and filter changes, and every 30 seconds while the
  UI's live connection is down), at most once every 2 seconds per server process.
- stemtrace is read-only: it has no equivalent of Flower's actions on workers and tasks.

## Removing stemtrace

Remove the `init_worker` call and redeploy. What stemtrace's event transport created on the
broker stays until you delete it: the Redis stream `<prefix>:events`, or the RabbitMQ exchange
`<prefix>.events`.
RabbitMQ deletes a server's queue on its own once it has been unused for `ttl` plus a minute.
