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

## Queue wait and runtime

`duration_ms` runs from a task's first event to its last, so it adds up the time the task waited
in the queue, the time it ran and any retry countdowns. The API's task and graph node responses
also split it up:

| Field | From | To |
|---|---|---|
| `queue_wait_ms` | the task being sent (its first PENDING or RECEIVED) | its first run starting (first STARTED) |
| `runtime_ms` | the last run starting (its STARTED) | the task finishing (SUCCESS, FAILURE, REVOKED or REJECTED) |
| `total_runtime_ms` | each run's STARTED | that run's RETRY, or the finish for the last run, added up |

- `queue_wait_ms` covers the first attempt only. A retry's wait includes its countdown, so it is
  in `duration_ms` but not here. It is set once the task starts, while it is still running.
- `runtime_ms` and `total_runtime_ms` are set once the task finishes, and are equal for a task
  that ran once. A run that Celery started again after losing the broker connection counts in
  `total_runtime_ms`.
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
- Each run's start and end come from the clock of the worker that ran it. Within one attempt, a
  run started again on another worker whose clock is behind can end before it started; that
  run counts as 0 (see the same-attempt limitation in
  [Worker, queue and exception type](#worker-queue-and-exception-type)).
