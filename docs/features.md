# Features

What stemtrace records about your Celery tasks, and what the dashboard shows.

## Features

**See What Happened**
- **Task Flow Graphs**: Visualize chains, groups, and chords as interactive DAGs ([Graphs](graphs.md))
- **Execution Timeline**: Track queued → started → retried → finished states
- **Full lifecycle tracking**: PENDING → STARTED → SUCCESS/FAILURE, with RECEIVED when `publish_received=True` is set on the worker ([Received events](configuration.md#received-events-opt-in))
- **Arguments & Results**: Inspect inputs, outputs, and exceptions
- **Task Output**: See what each attempt printed and logged, next to the task (opt-in, see [Task output](configuration.md#task-output-print-and-logging))
- **Retry Chains**: Understand exactly when and why retries happened
- **Worker, queue and exception type**: Which worker ran each task, which queue it came from, and the exception class of failures, with API filters ([details](#worker-queue-and-exception-type))
- **Real-time updates**: WebSocket-powered live dashboard

**Canvas Support**
- **Groups & Chords**: Automatic visualization of `group()` and `chord()` patterns
- **Parent-Child Tracking**: See which task spawned which

**Worker Monitoring & Registry**
- **Workers page**: See which workers are online/offline and what tasks they have registered
- **Registry status badges**: Quickly spot tasks that are active, never run, or not registered by any current worker
- **Task groups**: Every task belongs to a group, taken from its name or from your rules, with tags; in the API for now ([Task groups](configuration.md#task-groups))

**Production Ready**
- **Zero Infrastructure**: Uses your existing broker (Redis or RabbitMQ), no database needed
- **Sensitive Data Scrubbing**: Passwords and API keys filtered automatically ([Security](security.md))
- **Read-Only**: Safe for production; never modifies your task queue
- **FastAPI Integration**: Mount into your existing app with one line ([Deployment](deployment.md#embedded-in-fastapi))

## What you'll see in the dashboard

### Task details (timing, inputs/outputs, errors)

- **What you'll see**: Per-task execution timing (including how long it spent in each state), parameters (args/kwargs), return value, and the full event history.
- **Why it helps**: Quickly answer "what happened?" for a single task: slow queueing vs slow execution, which retry succeeded, and (on failures) the exception + traceback for debugging.

<p align="center">
  <img src="screenshots/task_details.png" width="900" alt="Task detail view showing parameters, result, and timeline" />
</p>

### Flow graphs (chains, groups, chords)

- **What you'll see**: An interactive DAG of your workflow with parent→child edges, plus clear GROUP/CHORD containers for Celery canvas patterns.
- **Why it helps**: Understand fan-out/fan-in at a glance (especially chords), spot which branch failed, and debug "why didn't my callback run?" without grepping logs.

<p align="center">
  <img src="screenshots/workflow.png" width="900" alt="Chord visualization in the workflow graph" />
</p>

### Task registry (registration status + warnings)

- **What you'll see**: A registry of tasks with status badges like **Active**, **Never Run**, and **Not Registered** plus "registered by …" worker info.
- **Why it helps**: Catch misconfigurations where tasks get stuck in **PENDING** because no current worker has the task registered (common in multi-repo or deploy drift scenarios).

<p align="center">
  <img src="screenshots/unregistered.png" width="900" alt="Task registry showing not-registered warning and status badges" />
</p>

### Workers and registry tabs

stemtrace uses **Celery inspect** on demand to populate workers and registered tasks, so those
pages work even if the server missed `worker_ready` events.

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

## Worker, queue and exception type

Task events record where a task ran, where it was sent and what it raised. The API's task, graph
node and event responses include them, with no configuration:

| Field | Example | On events |
|---|---|---|
| `hostname` | `celery@media-gpu-1` | RECEIVED, STARTED, SUCCESS, FAILURE, RETRY, REVOKED |
| `queue` | `media` | PENDING and the worker's events |
| `routing_key` | `media` | PENDING and the worker's events |
| `exception_type` | `ValueError`, `myapp.errors.PartnerAPIError` | FAILURE, RETRY |

- `hostname` is the worker's nodename, the same name the Workers page lists (also under
  `celery multi`). Tasks run eagerly with `apply()` have none.
- `exception_type` is the class only (`module.Class`, just `Class` for builtins), never the
  message, so it carries no task arguments. On a RETRY it is the exception the retry was for:
  the `exc` given to `self.retry()`, or else the exception being handled when it was called
  (`except ConnectionError: raise self.retry()`). A retry with neither (e.g. polling until
  something is ready) reports `celery.exceptions.Retry`, and one Celery cancelled after losing
  the broker connection has none.
- A task (in the task list, the task detail and graph nodes) has the value from its furthest-along
  event that reports one, judged the way its state is: a later retry beats an earlier one
  whatever the worker clocks say. Its `exception_type` is the last exception it raised, also
  when a retry then succeeded; add `state=FAILURE` to get failures only.
- `GET /api/tasks` filters on them exactly: `?hostname=celery@media-gpu-1`, `?queue=media`,
  `?exception_type=myapp.errors.PartnerAPIError`.

Limitations:

- `queue` is known only when Celery publishes straight to a queue: the default, and routes that
  name a queue (`task_routes`, `apply_async(queue=...)`) on a direct exchange. A route through a
  topic, fanout or other named exchange (an exchange and routing key, or a queue bound to a
  non-direct exchange) can reach any number of queues, so `queue` is empty and only
  `routing_key` is set. Checked with a topic exchange on real Redis and RabbitMQ workers.
- A FAILURE that Celery reports from the worker's main process, such as a hard time limit
  (`billiard.exceptions.TimeLimitExceeded`), carries no `hostname`, `queue` or `routing_key`,
  like its parent and root IDs. The task keeps the values of its furthest-along earlier event
  that has them, normally the STARTED of the run that failed. Within one attempt (the same
  retry count), events are ordered by time: when a lost broker connection makes Celery run the same attempt again on
  another worker whose clock is behind the first one's, the task can name the first worker.
- Run eagerly (`task_always_eager`) from inside an `except` block, a task's bare
  `raise self.retry()` reports the caller's exception being handled, since it runs on the
  caller's stack.
- `GET /api/tasks` filters on these fields by reading every event of every task, several times
  the cost of a name filter: on a full store of 10,000 tasks (6 events each) a filtered request
  took 40 to 130 ms against 10 to 30 ms for a name filter, on a loaded 2-vCPU host.

## Summary endpoints

Aggregates over the tasks the server holds, for dashboards and scripts that shouldn't page
through every task. A task counts at the time of its last event (the final event once it
finished) and under its current state, so a task that failed and then succeeded on retry counts
as a SUCCESS. Windows run from `within` (`30s`, `15m`, `6h`, `7d`; at most `3650d`) before now,
or from `from_date` (pass one or the other; a date or time without a timezone is UTC, and a
start after now is a `422`). Every response says which window it covered. A window ending now
also takes tasks stamped later by a worker whose clock is ahead.

- `GET /api/overview?within=1h` (default `1h`): finished runs, failures, runs per minute and the
  failure rate (FAILURE over finished runs; 0 with none), each with `baseline`, the same value for
  the window of equal length just before. Finished means SUCCESS, FAILURE, REVOKED or REJECTED.
  `workers` counts online and known workers from worker events, without asking the workers.
- `GET /api/stats/timeseries?bucket=5m&within=6h`: runs by state per bucket, as one array per
  state (states with no run in range are left out). Bucket edges are multiples of the bucket
  size since the Unix epoch, so they don't move between requests; the first bucket starts on
  the edge at or before the window's start, and the response's `window.start` is that edge.
  Without `within` or `from_date` it covers every task the server holds. Without `bucket` it
  uses the smallest of 1m, 5m, 15m, 1h, 6h, 1d and 7d that fits in 1,440 buckets; a `bucket`
  that needs more is a `422`. `group_by=task_group` adds the same counts per
  [task group](configuration.md#task-groups).
- `GET /api/failures?group_by=exception&within=1h`: tasks whose state is FAILURE, grouped by
  `exception_type`, most failures first. Each group has its count, first and last failure, task
  names and up to 5 of the latest task IDs. Tasks from workers that don't report the exception
  class form one group with `exception_type: null`.

Each request copies the server's list of task summaries once and computes outside the lock that
the event consumer uses, so it never holds up incoming events for longer than that copy.

The task list also takes:

- `state=FAILURE,RETRY`: any of several states, in any case. An unknown state is a `422` that
  lists the valid ones.
- `within=1h` (also on `GET /api/graphs`), as above.
- `compact=1` (also on `GET /api/tasks/{id}/children`): `events` is `null` instead of each
  task's event list. The task detail always includes them.

`GET /api/health` reports the Celery broker's kind as `broker`: `redis`, `rabbitmq`, `memory`,
or `null`. Only the URL's scheme is read; the URL itself is never returned.
