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
- **Queue wait and runtime**: How long each task waited before it started and how long it ran, apart from its overall duration ([details](#queue-wait-and-runtime))
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
- A worker is shown offline once 3 minutes pass without a sign of life from it: its
  `worker_ready`, an event from one of its tasks, or an inspect reply (the Workers page asks
  when it loads). The check runs as events arrive, so on a server that receives no events at
  all a stopped worker stays online until the next event or inspect. Limitations: a task that
  runs longer than 3 minutes without other events from its worker lets the worker show offline
  until its next event (stemtrace has no worker heartbeat; a solo worker can't answer inspect
  meanwhile either). A worker clock behind the server's shortens the 3-minute window by that
  much (3 minutes or more: task events don't help). Task event times ahead of the server's
  clock count as now; a `worker_ready` time ahead of it does not, so a worker killed within
  that much time of starting stays online longer. Workers older than 0.4.1 send no worker name
  on task events, so only inspect keeps them online.
- **Limitation: duplicate nodenames.** Two live workers that share a nodename (e.g. started
  with the same `-n`, which Celery itself warns about with `DuplicateNodenameWarning`) can't be
  told apart: Celery keeps only the last reply per nodename, so they flip between online and
  offline on every refresh. Give each worker a unique nodename.

## Ignored and rejected tasks

A task that raises Celery's `Ignore` ends as **IGNORED**, and one that raises `Reject` ends as
**REJECTED**, with the reason as its exception (shown on the task page). Both used to stay
STARTED, because Celery sends no failure for them. A `Reject(requeue=True)` publishes nothing on
a task with `acks_late`: the message runs again. Without `acks_late` the worker acknowledged the
message before the task ran, so the requeue does nothing and the task ends as REJECTED, as it
does under eager `apply()`. `task.replace()` raises `Ignore` internally, but the replaced task isn't
IGNORED: the replacement takes over its task ID and reports its own outcome. In a group, an
ignored task ranks just above SUCCESS and below every other state, so a group whose tasks all
succeeded or were ignored shows IGNORED. Workers older than this release send neither, and a
server older than this release drops IGNORED events with a warning per event, so upgrade the
server first.

## Worker, queue and exception type

Task events record where a task ran, where it was sent and what it raised. The API's task, graph
node and event responses include them, with no configuration:

| Field | Example | On events |
|---|---|---|
| `hostname` | `celery@media-gpu-1` | RECEIVED, STARTED, SUCCESS, FAILURE, RETRY, REVOKED, IGNORED, REJECTED |
| `queue` | `media` | PENDING and the worker's events |
| `routing_key` | `media` | PENDING and the worker's events |
| `exception_type` | `ValueError`, `myapp.errors.PartnerAPIError` | FAILURE, RETRY, REJECTED (when the reason is an exception) |

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

## Queue wait and runtime

`duration_ms` runs from a task's first event to its last, so it adds up the time the task waited
in the queue, the time it ran and any retry countdowns. The API's task and graph node responses
also split it up:

| Field | From | To |
|---|---|---|
| `queue_wait_ms` | the task being sent (its first PENDING or RECEIVED) | its first run starting (first STARTED) |
| `runtime_ms` | the last run starting (its STARTED) | the task finishing (SUCCESS, FAILURE, REVOKED or REJECTED) |
| `total_runtime_ms` | each run's STARTED | that run's RETRY, or the finish for the last run, added up |

- `queue_wait_ms` covers the first attempt only: a retry's wait includes the retry's countdown,
  so it is in `duration_ms` but not here. It is set once the task starts, while it is still
  running.
- `runtime_ms` and `total_runtime_ms` are set once the task finishes, and are equal for a task
  that ran once. A run that Celery started again after losing the broker connection counts in
  `total_runtime_ms`. Values are whole milliseconds, truncated.
- Fields are empty when the events they need are missing: `queue_wait_ms` without a PENDING
  (the code that sent the task doesn't run stemtrace) and without RECEIVED (off by default), or
  without the first STARTED; `runtime_ms` without the last run's STARTED, or for a task revoked
  before it started. GROUP and CHORD nodes have none.
- They are computed on the server from the events it already has, so they also appear for tasks
  recorded by older workers.

Limitations:

- `queue_wait_ms` compares the sending process's clock with the worker's. The sender stamps
  PENDING just after publishing, so a task a worker starts at once can look like it started
  first; a negative wait, from that or from a sender clock ahead of the worker's, counts as 0.
  A sender clock behind the worker's adds its offset to the wait. Turning on RECEIVED
  (`publish_received=True`) gives a start stamped by the worker's own clock, used when it is
  earlier.
- A first attempt sent with a `countdown` or `eta` waits for it on the worker, and that wait is
  in `queue_wait_ms`: events don't record the ETA, so the server can't tell it from time spent
  in the queue.
- `total_runtime_ms` leaves out runs whose end wasn't reported: a pool process lost while
  running (the task is redelivered with `acks_late`), or a run that ended in its own SUCCESS
  after `self.retry(throw=False)`. If the worker that runs a lost run again has a clock behind
  the first worker's by more than the time between the two starts, the lost run's STARTED is
  taken as the last run's start instead: `runtime_ms` and `total_runtime_ms` come out too short
  (0 at worst), and `queue_wait_ms` shorter. With `throw=False` and the second worker's clock behind the
  first's by more than the time between the two runs' ends, the first run's SUCCESS becomes the
  task's final event, and both fields are empty.
- A FAILURE Celery reports from the worker's main process (a hard time limit) doesn't say which
  attempt failed, and neither does the FAILURE of a first attempt that raised after
  `self.retry(throw=False)`: both carry retry count 0. If the events of the attempts in between were missed, the runtime is counted
  from the last STARTED the server has, so it can be too long.
- A span is counted as 0 when it would be negative: a worker's clock stepping back mid-run, or a
  span read across two workers' clocks (above). When Celery runs the same attempt again on
  another worker after losing the broker connection, and that worker's clock is behind the
  first worker's by about the gap between them, the runs can't be put in order: `runtime_ms`
  and `total_runtime_ms` are empty, and `queue_wait_ms` can come out shorter, by up to that offset (see the
  same-attempt limitation in [Worker, queue and exception type](#worker-queue-and-exception-type)).

## Summary endpoints

Aggregates over the tasks the server holds, for dashboards and scripts that shouldn't page
through every task. A task counts at the time of its last event in lifecycle order (a first
attempt's PENDING or RECEIVED stamped late by a sender whose clock is ahead doesn't move it) and
under its current state, so a task that failed and then succeeded on retry counts as a SUCCESS.
Windows run from `within` (`30s`, `15m`, `6h`, `7d`; at most `3650d`) before now, or, on the
time series and failures, from `from_date` (pass one or the other). A time without a timezone is
UTC. A date alone (`2026-10-18`) means its UTC midnight on current pydantic (checked with 2.13);
pydantic 2.4, the oldest supported, rejects it, as on the task list. A start after now is a
`422`. A window reaches at most 3,650 days back, whatever `from_date` or the oldest task says (a
time series then starts at the bucket edge at or before that, and the overview's baseline is the
window before it). Every response says which window it covered. A window ending now also takes
tasks stamped later by a worker whose clock is ahead.

- `GET /api/overview?within=1h` (default `1h`; it takes `within` only): runs, failures, runs per minute and the
  failure rate (FAILURE over runs; 0 with none), each with `baseline`, the same value for the
  window of equal length just before. Runs are tasks that ended with an outcome: SUCCESS, FAILURE,
  REVOKED or REJECTED. IGNORED tasks chose not to run (for example a deduplicated task), so they
  are left out, and a burst of them doesn't hide failures; the time series counts them by state.
  `workers` counts online and known workers from worker events, without asking the workers.
- `GET /api/stats/timeseries?bucket=5m&within=6h`: runs by state per bucket, as one array per
  state (states with no run in range are left out). Bucket edges are multiples of the bucket
  size since the Unix epoch, so they don't move between requests; the first bucket starts on
  the edge at or before the window's start, and the response's `window.start` is that edge.
  Without `within` or `from_date` it covers the last 3,650 days. Without `bucket` it
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
