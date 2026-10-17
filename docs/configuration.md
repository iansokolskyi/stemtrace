# Configuration

stemtrace has two sides to configure: the **workers** (and every other process that sends
tasks), set up with `stemtrace.init_worker(...)`, and the **server** that shows the dashboard,
either `stemtrace server` or `stemtrace.init_app(...)` inside your FastAPI app.

- [Worker options](#worker-options)
  - [Task output (print and logging)](#task-output-print-and-logging)
  - [Received events (opt-in)](#received-events-opt-in)
- [Server options](#server-options)
  - [Node names from task arguments](#node-names-from-task-arguments)
- [Embedded server options (`init_app`)](#embedded-server-options-init_app)
- [Environment variables](#environment-variables)

## Worker options

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

The keys scrubbed by default are listed in
[Security: sensitive data scrubbing](security.md#sensitive-data-scrubbing).

### Task output (print and logging)

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

### Received events (opt-in)

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

## Server options

```bash
stemtrace server \
    --broker-url redis://myredis:6379/0 \
    --transport-url redis://myredis:6379/0 \
    --host 0.0.0.0 \
    --port 8000 \
    --node-alias-from-arguments operator_type \
    --groups groups.toml \
    --root-path /monitoring \
    --reload  # For development
```

`--root-path` is for a reverse proxy that serves stemtrace under a path prefix; see
[Deployment: behind a reverse proxy](deployment.md#behind-a-reverse-proxy-with-a-path-prefix).
The login options (`--login-username`, `--login-password`, `--login-secret`) are in
[Security](security.md#protecting-the-standalone-server).

### Node names from task arguments

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

### Task groups

Every task belongs to one *task group* (a label for related tasks such as `billing` or
`search`; not Celery's `group()`). The API returns it as `task_group`, with the group's `tags`,
on tasks, graph nodes and the task registry, and `GET /stemtrace/api/groups` lists the groups.
The UI doesn't show groups yet.

**Automatic groups** come from the task name: the first module segment (`search.tasks.reindex`
→ `search`). Generic module names (`tasks`, `app`, `apps`, `celery`, `jobs`, `worker`, `workers`,
`src`, in any case) are skipped, so `tasks.search.reindex` and `app.billing.tasks.charge` give
`search` and `billing`. The last segment is the task's own name and never names a group: names
with no dots, or only generic modules (`add`, `tasks.add`, `app.tasks.add`,
`celery.chord_unlock`), are in `ungrouped`. Empty segments (`a..b`) are ignored. Only leading
generic segments are skipped, so when every task name starts with your project's package
(`myproject.billing.charge`, `myproject.search.reindex`) all tasks land in one group,
`myproject`: define groups in code for such layouts.

**Code-defined groups** take precedence. Each has a `name`, `match` (glob patterns on the full
task name, case-sensitive; `*` also matches dots), optional `tags` and an optional `color`
(`#rgb` or `#rrggbb`). The first group whose patterns match a task wins, in the order given;
tasks no group matches keep their automatic group. A task whose automatic group has the same
name as a code-defined group is in that group and gets its tags.

```python
from stemtrace import GroupRule

stemtrace.init_app(
    app,
    broker_url="redis://localhost:6379/0",
    groups=[
        GroupRule(name="billing", match=["billing.*", "*.charge_*"], tags=["money"]),
        {"name": "search", "match": ["search.*", "indexer.*"], "color": "#2563eb"},
    ],
)
```

Or put them in a TOML file and pass its path: `stemtrace server --groups groups.toml`,
`STEMTRACE_GROUPS=groups.toml`, or `init_app(groups="groups.toml")` (an explicit `groups=`
wins over `STEMTRACE_GROUPS`):

```toml
[[groups]]
name = "billing"
match = ["billing.*", "*.charge_*"]
tags = ["money"]

[[groups]]
name = "search"
match = ["search.*", "indexer.*"]
color = "#2563eb"
```

The rules are checked at startup: an unknown key (`matches`, `tag`), a missing `match`, an
invalid color or two groups with the same name stop the server with a `ConfigurationError`
(`stemtrace server` exits with a usage error). A `GroupRule(...)` you build yourself is checked
when you build it and raises pydantic's `ValidationError`. `create_router(groups=...)` takes the
same values and checks them when the router is created. TOML files need Python 3.11+ (stdlib `tomllib`);
on Python 3.10, define the groups in code. Groups are a **server** setting, like node aliases;
workers don't need them.

`GET /stemtrace/api/groups` returns each group's `name`, `source` (`code` or `auto`), `tags`,
`color` and `task_names`: the task names the server has seen in events or in workers'
registered tasks, as in the task registry. Code-defined groups are listed even before any of
their tasks has run. Groups are sorted by name, with `ungrouped` last.

## Embedded server options (`init_app`)

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
    groups=None,                # Task group rules or a TOML path; see Task groups
)
```

Authentication (`auth_dependency` and the built-in login) is described in
[Security](security.md#protecting-an-embedded-dashboard).

## Environment variables

You **do not need** these environment variables if you're using the Python APIs:
- `stemtrace.init_worker(app, transport_url=..., prefix=..., ttl=...)`
- `stemtrace.init_app(app, broker_url=..., transport_url=...)`

They exist mainly as **convenient defaults** for:
- `stemtrace server` / `stemtrace consume`
- container/Docker setups where passing flags is awkward

If your app already uses env vars like `BROKER_URL`, `REDIS_URL`, etc., pass them through:

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
| `STEMTRACE_ROOT_PATH` | Server only: URL prefix stripped by a reverse proxy in front of `stemtrace server` (see [Behind a reverse proxy](deployment.md#behind-a-reverse-proxy-with-a-path-prefix)). Same as `--root-path`. | Unset. |
| `STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS` | Server only: show a task argument as the graph node name (see [Node names from task arguments](#node-names-from-task-arguments)). Same as `--node-alias-from-arguments` / `init_app(node_alias_from_arguments=...)`. | Unset (task names are shown). |
| `STEMTRACE_GROUPS` | Server only: path to a TOML file of code-defined task groups (see [Task groups](#task-groups)). Same as `--groups`; `init_app(groups=...)` wins over it. Python 3.11+. | Unset (automatic groups only). |

`STEMTRACE_LOGIN_SECRET` and `FORWARDED_ALLOW_IPS` are described with the built-in login in
[Security](security.md#built-in-login-page).
