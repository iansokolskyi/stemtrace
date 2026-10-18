# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

**Highlights:**
- Task groups: every task now belongs to a group (automatic from its module, or defined in code or a TOML file), returned by the API and listed by `GET /api/groups`. The UI shows them in the next release.
- Task events now record which worker ran a task, the queue it was sent to and the exception class, and `GET /api/tasks` can filter on all three.
- Fixes: busy workers no longer show offline after about 3 minutes; a cold worker shutdown (SIGQUIT) during an event publish no longer hangs the worker; dict keys that aren't strings no longer lose a task's events; custom sensitive keys now match in any letter case.
- The README is now a short overview, with the details on pages under `docs/`, including a new guide to adopting stemtrace in an existing project.

**Upgrading:** in any order. The new event fields are empty for events from older workers, and an older server ignores them. The minimum `typer` is now 0.16.0.

### Added
- Task groups: every task belongs to one group, returned as `task_group` (with the group's `tags`) on the API's task, task detail, graph node and task registry responses, and listed by the new `GET /api/groups` (name, source `auto`/`code`, tags, color, task names seen). Automatic groups come from the task name's first module segment, skipping generic ones (`tasks.search.reindex` and `search.tasks.reindex` are both `search`; `add`, `tasks.add` and `celery.chord_unlock` are `ungrouped`). Code-defined groups, matched by glob on the task name with the first match winning, come from `init_app(groups=[GroupRule(...)])` / `StemtraceExtension(groups=...)`, or a TOML file given by `stemtrace server --groups`, `STEMTRACE_GROUPS` or `init_app(groups="file.toml")`; invalid rules fail at startup. TOML files need Python 3.11+. The UI doesn't show groups yet. See [Configuration: task groups](https://github.com/iansokolskyi/stemtrace/blob/main/docs/configuration.md#task-groups).
- Task events record the worker's nodename (`hostname`), the `queue` and `routing_key` the task was sent with and, on failures and retries, the exception class (`exception_type`, e.g. `myapp.errors.PartnerAPIError`; for a retry, the exception it was for). The API's task, graph node and event responses include them, and `GET /api/tasks` filters on `hostname`, `queue` and `exception_type`. `queue` is empty for tasks routed through a topic or other named exchange, which can reach several queues. See [Features](https://github.com/iansokolskyi/stemtrace/blob/main/docs/features.md#worker-queue-and-exception-type). Upgrading: in either order. A server from before this release ignores the new fields, and this release's server shows them as empty for older workers' events; tasks get them once workers (and, for PENDING, the code that sends tasks) run this release.

### Changed
- Minimum `typer` is now 0.16.0 (was 0.13.0). With click 8.2 or newer, which nothing kept out, `stemtrace --help` and `stemtrace server --help` failed with `TypeError: Parameter.make_metavar() missing 1 required positional argument: 'ctx'` on typer before 0.16.0 (0.15.4 instead holds click below 8.2). The lowest-dependencies CI job now runs `--help` against the floor.
- UI: updated `react` and `react-dom` 19.2.3 → 19.3.0 (with `scheduler` 0.27.0 → 0.28.0), `@xyflow/react` 12.10.0 → 12.12.0 (with `@xyflow/system` 0.0.74 → 0.0.83) and `date-fns` 4.1.0 → 4.4.0 in the bundled frontend. None of these fix a known advisory. Development: the frontend now type-checks with TypeScript 7.0 (was 5.9) and runs its E2E tests with Playwright 1.63.
- Docs: the README is now a short overview (what stemtrace helps you debug, what it adds to a production system, when Flower or other tools fit better, quick start), and the details moved to pages under [`docs/`](https://github.com/iansokolskyi/stemtrace/tree/main/docs): configuration, deployment, security, brokers, event retention, graphs and features. A new page covers adopting stemtrace in an existing project.

### Fixed
- A worker could hang on cold shutdown instead of exiting. With Celery 5.5 or newer, a SIGQUIT (or a second Ctrl+C) cancels running tasks from inside Celery's signal handler, which makes stemtrace publish a RETRY event. If the worker's main process was itself publishing an event at that moment and holding a lock on the publish path (the publish breaker's, which it takes during an event-broker outage or right after events were dropped), the second publish waited for that lock forever, until another signal or a SIGKILL. A publish that interrupts another one on the same thread is now dropped instead. The one cost is that, in this race, the cancelled task's RETRY event is lost. Reproduced with a prefork worker on Redis; the RabbitMQ transport uses the same breaker, and gevent and eventlet workers reach the same code. Known limitation on the gevent pool: a cold shutdown that lands while a task's greenlet is running Python code (not waiting on I/O) can be interrupted while stemtrace publishes that task's RETRY, and the worker keeps running; send SIGQUIT again or SIGKILL. See [Stopping workers](https://github.com/iansokolskyi/stemtrace/blob/main/docs/deployment.md#stopping-workers).
- Workers: a worker busy running tasks no longer shows offline about 3 minutes after it started. Its task events now count as signs of life (only `worker_ready` and inspect replies did), so `/api/workers?refresh=false` and the Workers page between refreshes show it online while its tasks send an event at least every 3 minutes. A single task running longer than that still lets it show offline until its next event (there is no worker heartbeat), and a worker clock behind the server's shortens the 3-minute window by the difference. A worker that stops still goes offline once 3 minutes pass without its events (checked as other events arrive, as before), and a task event never brings back a worker that reported `worker_shutdown`. Workers older than this release send no worker name on task events and are still kept online only by inspect. See "Workers and registry tabs" in `docs/features.md`.
- Graphs: a chord whose node was removed (`TaskGraph.remove_node`) while its headers stayed lost its callback: when a later event of one of its tasks recreated the node, it no longer knew the callback the headers had named, so it drew none. It now draws the callback a graph built from the same events draws. Removing the last header after the node no longer leaves that callback ID recorded for the chord.
- Graphs: a chord's callback and a group's chain links no longer depend on event order when an earlier event renames a task to or from `celery.chord_unlock` (one task ID reporting two names), or when the parent of a chord's body group changes while that group is drawn as the callback. Celery workflows don't produce these events; they showed up when comparing removals with rebuilds.
- A task whose arguments or result hold a dict with a key that isn't a string (an int, `None`, a tuple, bytes) no longer loses its events. Scrubbing raised `AttributeError` on the key, so the PENDING event (`.delay({1: "a"})`), the STARTED event (pickle workers; json turns the keys into strings) and the SUCCESS event (such a return value) were dropped, and Celery logged an error for each. Bytes keys are now matched against the sensitive keys like strings (decoded as UTF-8), tuple and frozenset keys by their parts, Enum members by their name or value, and Flag values by the members they contain; int, `None` and other keys never match, and dicts inside their values are still scrubbed. Task data stemtrace can't capture (a value whose `str()` raises, a cyclic structure) now shows as `[Not captured: <error type>]` and the event is still published, and keyword arguments with non-string keys (`send_task` doesn't reject them) are shown with the key as a string when json can encode them (int, `None`, float keys; tuple or bytes keys show the kwargs as text).
- `additional_sensitive_keys` (and the `sensitive_keys`/`additional_keys` arguments of the scrubbing functions) are now lowercased before matching, like the keys (`str.lower()`). A pattern with an uppercase letter, such as `{"MRN"}`, never matched any key before, so those values were published unscrubbed.
- Keyword arguments that json can't encode are now shown as `{"_as_text": "<their text>"}` instead of `{"_truncated": ...}`; `_truncated` now only holds the `[Truncated: N bytes > M max]` message for keyword arguments that were too big.

## [0.4.0] - 2026-10-13

**Highlights:**
- Task graphs match what Celery runs: chains with chords, groups of chains, chord bodies, `task.replace()` (any event order and worker clock skew), and the graph page updates live.
- Workers never wait on a broken event broker: Redis gets the circuit breaker RabbitMQ has, DNS lookups are bounded, and RabbitMQ reuses one connection per process (about 0.3 ms per event instead of 8 ms).
- Celery soft time limits are no longer swallowed by stemtrace, and the Workers page lists each worker once across restarts and `celery multi`.
- The built-in login throttles failed sign-ins and works behind a path-prefixing proxy.
- Minimum versions are now `celery>=5.2.2`, `pydantic>=2.4.0`, `kombu>=5.2.2` and `typer>=0.13.0`. RECEIVED events are opt-in (`init_worker(publish_received=True)`).

**Upgrading:** upgrade the server before (or together with) the workers. This release's workers put the parent and group IDs on the PENDING event they send. A 0.3.10 server shows more tasks twice when it gets those events: on Celery 5.2, both headers of `chain(a, chord([b, c], d))` also appear directly under `a` (one does with older workers). This release's server draws the same graphs from older workers' events as from this release's (checked on every captured test workflow).

### Added
- Built-in login: failed sign-ins are throttled in memory, per client IP (5 failures; IPv6 per /64) and per username (20, from all IPs) or, for browsers that signed in before, per device (5). Further attempts get 429 with `Retry-After`, starting at 30 seconds and doubling up to 15 minutes. A successful sign-in sets a signed device cookie (`stemtrace_session_device`, 90 days with a configured `STEMTRACE_LOGIN_SECRET`) so strangers' failures, by username or from a shared IP, can't lock out browsers that have signed in before; anyone who knows the username can still keep it locked for other browsers. Limits are per process, and the client IP comes from the ASGI server (behind a proxy, list its addresses in uvicorn's `--forwarded-allow-ips` / `FORWARDED_ALLOW_IPS`, never `*`). See the README.
- `stemtrace server --root-path /prefix` (or `STEMTRACE_ROOT_PATH`) for a reverse proxy that serves stemtrace under a path prefix and strips it; it is passed to uvicorn as `root_path`. Prefixes the UI can't use (characters other than letters, digits, `.`, `_`, `-`) are rejected at startup.
- `init_worker(app, publish_received=True)` (off by default) makes workers publish a RECEIVED event when they take a task message, so tasks show when they reached a worker before starting, e.g. while waiting for an ETA or a free pool slot. The step was only ever published in tests before. It is opt-in because it adds one synchronous publish per message to the worker's consumer loop, which under prefork is the main process: about 0.7 ms per message on RabbitMQ, about 22% of the throughput of no-op tasks (prefork, with connection reuse; Redis was not measured). With it off nothing is added to the consumer loop. It is published for each delivery of a task's first attempt: a retry's message adds none (its `RETRY` event covers it), but a message redelivered after a lost worker (`acks_late` with `reject_on_worker_lost`) adds a second one, and both are listed ahead of the task's `STARTED` events. For protocol 1 chord header tasks it no longer fails on the chord signature. Verified with the prefork, threads, solo, gevent and eventlet pools.

### Changed
- Minimum dependency versions raised: `celery>=5.2.2` (5.2.2 fixes CVE-2021-23727), `pydantic>=2.4.0` (2.4.0 fixes CVE-2024-3772) and `typer>=0.13.0`, the lowest the test suite passes with; `kombu>=5.2.2`, which stemtrace imports, is now declared. A new CI job runs the unit tests against these minimums.
- Graph data: a node's `parent_id` is where it sits in the graph. For a group's task that is the GROUP/CHORD node, for a chord's callback the CHORD node, and a task whose parent isn't in the store yet has none. The parent a task's events reported is in the new `reported_parent_id` field, on `TaskNode` and in the API's task and graph node responses.
- `TaskGraph.root_ids` is now computed and returns a copy: appending to it no longer changes the graph. `TaskGraph.model_validate` rebuilds the graph from its nodes' events, so a deserialized graph keeps its groups and links and accepts more events.
- RabbitMQ: each worker process now publishes events on one connection that it opens with its first event and keeps, instead of opening, using and closing a connection per event. On a local broker an event takes about 0.3 ms instead of about 8 ms, and a prefork worker that publishes an event per received message (the cost that motivated this) ran about six times faster in the same measurement (4 children; 40 to 250 tasks per second on a loaded 2-vCPU host). Prefork children open their own connection and never use the master's; threads, gevent and eventlet pools share their process's connection and publish one event at a time; a connection the broker closed while idle is noticed before the next event and replaced without losing it. The connection is named `stemtrace publisher <host> pid <pid>` in RabbitMQ's management UI, negotiates no AMQP heartbeat (a `?heartbeat=` in the URL no longer applies to publishing), and is closed when the interpreter or a prefork child exits. An event never waits more than 2 s for another publish to finish (it is dropped, without counting as a failure); the one that has to connect takes at most about 20 s with one URL host (14 s more per extra host) against a broker that answers every step just within its 2 s timeout, and milliseconds against a healthy one. A connection lock that is never released (a signal handler raised at the wrong moment) is replaced with a new connection, at once if the same thread publishes again and otherwise once its holder has had the longest an attempt can take, with the warning `RabbitMQ publisher connection was not released by its last user; starting over with a new connection`. Limitations are in the README (an event written as the broker closes the connection can be lost; deleting the exchange loses the next event).
- `RabbitMQTransport.close()` closes the process's publisher connection; the next publish connects again.
- Login: a `next` target containing a backslash or a control character now falls back to the default page after sign-in. This is hardening: Starlette percent-encodes the redirect, so such targets didn't lead off-site.
- `WorkerRegistry.mark_shutdown` now records a `worker_shutdown` event, which an inspect ping can't undo; `register_worker` (a `worker_ready` event) now takes the node's other pids offline. For a worker that merely stopped answering use the new `mark_offline`.

### Deprecated
- `stemtrace.transports.rabbitmq.PUBLISH_SLOW_ATTEMPT` (also exported by the deprecated `stemtrace.library.transports.rabbitmq`) is no longer used, since a single slow publish failure no longer opens the circuit breaker; accessing it emits a `DeprecationWarning`, and it will be removed in 1.0.
- `PublishBreaker(slow_attempt=...)` and `PublishBreaker.record_failure(elapsed=...)` are ignored and emit a `DeprecationWarning`; they will be removed in 1.0.

### Removed
- `WorkerRegistry.mark_online` (reachable through `StemtraceExtension.worker_registry`). A worker that answered an inspect call is now brought online with `mark_node_alive`, which applies the one-live-process-per-nodename rule.

### Fixed
- RabbitMQ: the server's consumer now sends AMQP heartbeats when the broker URL has `?heartbeat=N`. Its read loop never did, so RabbitMQ closed the idle consumer connection after two intervals and it reconnected with a warning every few seconds (`heartbeat=1` or `2`: first drop within 10 s on RabbitMQ 3). The consumer also reconnects when the broker's own heartbeats stop, including a broker frozen with its TCP connection still up (`docker pause`): a connection that failed is dropped without the AMQP close handshake, which would otherwise wait forever for the frozen broker's reply and leave the consumer thread unable to stop. URLs without `heartbeat` (the default) are unchanged.
- Redis: a tiny `socket_timeout` in the broker URL (e.g. `?socket_timeout=0.001`) no longer makes the server's consumer loop spin: its idle reads returned within milliseconds and looped straight back (measured against Redis 7: 130 reads in 2 s and a quarter of a core for `0.001`). An idle read (an empty reply or a read timeout) now takes at least 0.25 s (4 reads a second when idle), so with any `socket_timeout` under 0.5 s (the XREAD block window is half of it) a new event can be delayed by up to that long; with the default timeouts nothing changes. Repeated read timeouts still log the same warnings.
- Graphs: events in which two chords share one `celery.chord_unlock` task or one group-body start (Celery workflows don't produce these) no longer crash event handling; the drawing can still depend on event order (see the README's graph limitations).
- Graphs: a group's tasks are listed only inside the group, which sits under the task that started it; they used to also appear directly under that task once they started.
- Graphs: a task or group whose parent task isn't in the store (not received yet, or evicted) is now listed as a graph of its own instead of being shown in no graph, and moves under its parent once the parent arrives. A task's `parent_id` is empty while it is listed that way.
- Graphs: a group or task no longer stays listed under a parent it moved away from, a chord sits under the task that started it, and a task can no longer end up linked under itself (directly or through a cycle).
- Graphs: a chain containing a chord, such as `chain(a, group(b, c), d, e)`, is one graph again: the chord sits under `a`, `d` is drawn as the chord's callback and `e` follows it. Celery stamps the chord's group ID on the chain's last task, which used to count as a header and split the workflow in two. The same applies to a chord whose body is a chain (the body's first task is the callback; its later tasks no longer appear inside the chord) or another chord (drawn as a container after the outer chord). Verified with Celery 5.6.3; on Celery 5.2 the headers of `chain(group(...), task)` and of a chord with a chain body carry no chord callback ID, so those still show as a group, with the chain hanging off the header that finished last, inside the group.
- Graphs: a chord's callback hangs off the chord instead of off the header that finished last, and a retried header stays a header.
- Graphs: `group(chain(a1, b1), chain(a2, b2))` is one graph: each chain is drawn as a row inside the group (Celery only marks the chains' last tasks as members). A parent task with other children is never pulled into a group. Other groups whose tasks report different parents go under the one most of them report, the same one whatever order events arrive in.
- Graphs: a chord inside a chain, such as `chain(a, chord([b, c], d))`, draws its callback again (it reports `a`, not a header, as its parent).
- Graphs: a chain's first task is no longer drawn inside a group for a moment before moving out, when the sending side runs this release (with older senders it can still happen if the task's other children arrive late); `group(chain(a, b), c)` draws the chain inside the group; a header's own subtasks (or a group it starts) are drawn next to the chord instead of not at all.
- Graphs: a chord whose body is a group, such as `chord([a, b], group(c, d))` (or `chain(group(a, b), group(c, d))`, which Celery turns into one), draws the body as a group after the chord, whatever order events arrive in; one body task used to be drawn as a header's subtask.
- Graphs: `task.replace()` no longer makes the graph depend on event order or on the workers' clocks. Celery gives the replacement's last task the replaced task's ID, so that ID reports two names and parents (the worker even announces it with a second PENDING); a replaced task's name and parent now come from its own events, ignoring those that report a task below it (or, for a single-task replacement, itself) as parent, and it stays where it was sent. Checked with captured replacements by a task, a group, chains of 2 to 12 tasks inside a chain (and a 300-task chain built from them) and short chains as a workflow's root, a chord header or body and inside a group, with nested replacements and recursive pagination, in shuffled orders and with the replacement's clock skewed by up to an hour either way.
- Graph page: sibling chords whose headers send subtasks no longer draw those subtasks on top of the chords' callbacks.
- Worker events: the sender's PENDING event now carries the parent, root and group IDs (taken from `after_task_publish`; Celery's deprecated `task_sent` signal, used until now, never passes them), so tasks are placed under their parent and in their group as soon as they are sent. The set of task IDs a process already announced is now bounded (10,000 most recent), so processes that only send tasks (web apps) no longer grow it forever, and a non-numeric `retries` header from a non-Celery producer no longer raises.
- Graphs: a chord's callback is only drawn on evidence from the events (the callback the headers name, or the chord's group ID Celery stamps on a chain body's last task, traced back to a header). A header's own subtask or sub-group is no longer drawn as the callback, including when a header failed and the body never ran; such a chord shows no callback. Chords run through Celery's `chord_unlock` task (result backends without chord support) link their callback too.
- The server no longer evicts a group's or chord's node before its tasks; a recreated group used to be linked under itself, which left its graph page blank. Evicting a task also drops it from its group and from its chord.
- Graph page: a malformed graph in which a group lists itself among its children is drawn instead of rendering blank.
- Group state: a group with a `REJECTED` task shows `REJECTED` (ranked right below `FAILURE`) instead of depending on the order of its tasks.
- Graph page: the graph now updates while it is open. New tasks, new links and state changes (arriving by WebSocket or polling) used to show only after a reload. A node you dragged keeps its place (also when an update arrives while you hold it, and the view does not re-fit until you let go), and the view re-fits to show new tasks, once they are drawn, until you pan or zoom it (by dragging the canvas, with the mouse wheel or with the zoom buttons; a click on the canvas does not count).
- Live pages: when events arrived faster than the server answered (every 100 ms against a 300 ms response in the test), each event cancelled the refetch already running, so the graph page and the task list showed nothing new until the burst ended. They now refetch one round at a time, and events that arrive meanwhile share the next round.
- Registry: "View runs" lists only the runs of that task. It searched for the task's last name segment as a substring, so `app.add` also showed `app.add_numbers` and `other.app.add`. The tasks API takes a new `name_exact` parameter (whole task name, case-sensitive) next to `name`, which stays a case-insensitive substring match; the Tasks page shows the exact name as a removable "Task:" filter.
- UI: tasks whose names end the same (`app.billing.send` and `app.email.send` both showed as `send`) are told apart. The graph, the timeline, a task's child list, the registry and the workers page show the shortest dotted suffix that no other name on the page has (`billing.send`, `email.send`), with the full name as a tooltip (and, on graph nodes and timeline links, in the accessible name); a name that nothing else ends like still shows only its last segment.
- Server: handling events no longer scans every stored node for chord callbacks, which made each event slower as the store filled (about 3.5 ms per event at 10,000 nodes, now about 0.06 ms).
- Server: an event for a task in a large group no longer rescans the whole group (a 2,000-task group took 5.6 s to ingest, now 1.0 s), a chord's callback is no longer re-derived from all its headers on every event (2,000 headers with a subtask each: 7.3 s, now under 1 s) and a long chain body costs the same in any arrival order, and a long chain of tasks that schedule the next one no longer slows down as it grows (10,000 tasks: 30 s, now 3.3 s).
- Celery's `SoftTimeLimitExceeded` (and its other time-limit and termination exceptions) is no longer swallowed by stemtrace when it lands while a task is publishing an event or, with `capture_output`, while `print()` or logging is captured. RabbitMQ and Redis publishing used to log it as `Failed to publish event` and count it as a broker failure toward the circuit breaker (and a limit hitting the RabbitMQ pre-publish read made the publisher reconnect and carry on); capture swallowed it silently, so the task never saw its soft limit. These paths now re-raise it, and the breaker is not charged. Limit: Celery's own signal dispatcher logs and drops an exception a signal handler raises, so a limit that lands during the publish a task signal (`task_prerun`, `task_postrun`, ...) runs is logged by Celery as `Signal handler ... raised`, not delivered to the task.
- RabbitMQ: during a memory or disk alarm, the worker no longer leaves a blocked connection on the broker with every circuit-breaker retry (about 5 per worker process in the first minute and 2 a minute after that, until the alarm ended). RabbitMQ tells the publisher's connection that it is blocked; the worker drops events without sending them while it is, and its retries only read that connection.
- `init_app()` with the built-in login behind a path-prefixing reverse proxy (ASGI `root_path`, e.g. `uvicorn --root-path /monitoring`): the API and UI under the prefix were served without a session, and the login redirect and session cookie path left out the prefix. The login check now sees paths the way routing does, and redirects, `next` targets, the cookie path and the UI's API and asset URLs include the root path, also on uvicorn versions before 0.26 (which leave it out of the request path).
- UI: an asset path outside the bundle's own `assets/` directory (e.g. `/stemtrace/assets/<file>` on `stemtrace server`, which serves the bundle at `/assets/`) now returns 404, with or without a session, instead of the HTML app shell or a login redirect.
- Redis: publishing from workers no longer waits 5s per event (redis-py's default timeout) while Redis is down, hung or unreachable. Workers publish through a separate connection with 2s timeouts and no retries, and three failures in a row open the same circuit breaker RabbitMQ uses, which drops events (instead of queueing them) and retries once per cooldown (1s, growing to 30s). A reply slower than 2s counts as a failure; an event whose reply timed out after it was sent is reported as "sent but unconfirmed", since Redis may still have written it. The server's consumer keeps its timeouts.
- Redis and RabbitMQ: publishing no longer blocks a task for the DNS resolver's own timeouts (20s measured with an unreachable resolver). Workers resolve the broker host in a background thread, wait at most the 2s connect timeout, and cache answers for 30s with background refresh (an immediate refresh after a failed connect, so a DNS failover is picked up on the next attempt; a failed first lookup is retried after 5s). A process that forks (the Celery prefork master) waits briefly for its lookups before forking and then resolves in a short-lived helper process, so a child can't inherit a lock from glibc's DNS code; under uWSGI, whose forks skip Python's fork hooks, lookups run on the calling thread in every process (without the 2s bound, as before). TLS behavior is unchanged.
- RabbitMQ: a single timed-out publish no longer opens the circuit breaker (dropping events for a cooldown); like quick failures, it takes three failures in a row. A healthy broker can miss a deadline now and then, e.g. over TLS while another worker thread holds the GIL.
- Redis: a `unix:///path?...` broker URL with `ssl_cert_reqs` in its query string is no longer turned into `unix:/path?...` (which redis-py 4.5+ rejects) when stemtrace normalizes `ssl_cert_reqs`, for the server's consumer as well as for publishing.
- Redis: unknown or misspelled options in the broker URL (e.g. `?sockt_timeout=5`) are now ignored for publishing, with a one-time warning naming them; redis-py rejects them, which used to make every publish fail. The server's consumer still rejects them.
- Redis: each worker process (e.g. each prefork child) now builds its own publish connection pool instead of reusing one inherited from the parent, which could hang the child's first publish if another parent thread held the pool's lock at fork.
- Redis and RabbitMQ: worker event ids in publish failure logs read `host:pid:worker_ready` instead of `host:pid:WorkerEventType.WORKER_READY`.
- Workers page: each worker was listed twice once the server refreshed worker status from Celery, online under its bare host with no tasks (when `inspect registered` didn't answer) and offline under its nodename (`celery@host`, or `w1@host` for `celery multi`) with all its tasks. Workers are now always identified by their Celery nodename, so `celery multi` nodes on one host are also told apart. A nodename has one live process: when a node restarts, only its newest process can show as online. Its older processes go offline as soon as the new one's `worker_ready` arrives, even one that was killed without a shutdown event, and a ping reply never brings back a process that reported `worker_shutdown`. An inspect reply that was produced before a worker event arrived (inspect waits up to 0.5 s per call) no longer overrides that event. Limitation: two live workers with the same nodename (Celery warns with `DuplicateNodenameWarning`) can't be told apart and flip between online and offline on every refresh.

## [0.3.10] - 2026-10-11

### Changed
- Docker image: the UI is built once on the build machine's native platform instead of under emulation for each architecture, which also makes release builds faster. Building the image from source now needs BuildKit (the default since Docker 23 and in `docker compose`).
- Development: ruff now also enforces complexity, function-size, blind-`except` and dead-code rules, and [import-linter](https://import-linter.readthedocs.io/) checks the `core`/`library`/`server` import boundaries in `make lint` and CI (see CONTRIBUTING.md). No runtime change.
- The broker transports moved from `stemtrace.library.transports` to `stemtrace.transports`, so the Celery integration and the server share them without importing each other. The old module paths (`stemtrace.library.transports` and its `redis`, `rabbitmq`, `memory` and `breaker` submodules) still import the same public classes and functions but emit a `DeprecationWarning`, and will be removed in 1.0; patching module internals through the old paths (e.g. `mock.patch("stemtrace.library.transports.redis.time.sleep")`) no longer reaches the real code, so update such patches to `stemtrace.transports`. The transports' loggers are renamed to match (e.g. `stemtrace.transports.rabbitmq`); logging configured for the `stemtrace` logger is unaffected.

### Fixed
- Redis: a stream entry whose payload is valid JSON but not an object (e.g. `42`) is now skipped with a warning; it used to end the consumer's read loop and trigger a restart.

## [0.3.9] - 2026-10-10

### Added
- Task output: with `init_worker(app, capture_output=True)`, what each task attempt prints and logs is shown on its task page, one block per attempt (latest first). Off by default because output isn't scrubbed; each attempt keeps the last `max_output_size` bytes (default 16 KB). Works with prefork, thread and gevent pools (thanks @JonatanMartens for the original implementation — #49; requested by @ohadmata — #47).

## [0.3.8] - 2026-10-10

### Fixed
- `node_alias_from_arguments` can now be set on the standalone server and Docker image: `stemtrace server --node-alias-from-arguments <key>` or `STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS=<key>` (a kwarg name or a positional index). `init_app()` reads the same env var when the argument isn't passed (#69).
- RabbitMQ: publishing from workers no longer blocks tasks while the broker is down, stalled or in a memory/disk alarm. Each publish tries every host of the URL once with a 2s timeout; repeated or slow failures open a circuit breaker that drops events (instead of queueing them) until the broker answers again, and the worker logs at most four warnings a minute with counts of dropped events. The README's broker section has the details and limits.
- RabbitMQ: the server's consumer now reconnects after the broker restarts or drops the connection; it used to retry forever on a dead channel.
- RabbitMQ: a broker outage no longer floods the server log with one ERROR traceback per second; reconnects back off from 1s to 30s and log one line per attempt, plus one when the connection is back (thanks @ohadmata — #70, #71).
- RabbitMQ: a message the consumer can't decode is dropped with a warning instead of blocking the queue forever.
- RabbitMQ: stopping the server no longer waits out the reconnect delay or hangs on a frozen broker.

### Added
- Health endpoint: `consumer_connected` is now reported for RabbitMQ, so `/api/health` shows `degraded` while the server can't read from the broker.

### Deprecated
- `init_worker(node_alias_from_arguments=...)` has no effect (node names are resolved by the server) and now logs a deprecation warning; it will be removed in a future major release.

## [0.3.7] - 2026-10-10

### Changed
- Docker: the image's `HEALTHCHECK` now calls the new `<prefix>/api/health/live` endpoint instead of `/stemtrace/api/health`, which returns 401 under form login (so containers with login enabled reported unhealthy). The liveness endpoint needs no session and checks only that the process is serving: it always answers 200, with an informational `{"status": "ok"}`, or `{"status": "degraded"}` by the same rule as `/api/health` (the event consumer stopped unexpectedly or can't read from the broker; a consumer stopped on purpose or whose event source finished is not degraded).
- Docker image: updated the locked runtime dependencies `fastapi` 0.141.1 → 0.142.2 (which now pulls in `opentelemetry-api` 1.45.1; stemtrace doesn't configure tracing, so it stays a no-op), `pydantic` 2.12.5 → 2.13.5 (`pydantic-core` 2.41.5 → 2.46.5), `celery` 5.6.0 → 5.6.3, `typer` 0.21.0 → 0.27.3, `typing-extensions` 4.15.0 → 4.16.0 and `websockets` 15.0.1 → 16.1.1 (used by uvicorn's WebSocket support). Only the lockfile changed: the package's dependency ranges are the same, so pip installs are unaffected. With typer 0.27, `--help` shows option types as `<str>`/`<int>` instead of `TEXT`/`INTEGER`.
- Docker image: building the image from a local checkout no longer copies the frontend's `node_modules/`, `dist/` or test artifacts into the build context, so stale local UI assets can't end up in the image.
- UI: updated `@tanstack/react-query` 5.94.5 → 5.104.1 in the bundled frontend.

### Fixed
- Form login: clear warnings and an in-app explanation when sessions are rejected because workers/replicas don't share `STEMTRACE_LOGIN_SECRET`. Without an explicit secret each process signs cookies with its own random secret, so under `uvicorn --workers N`, gunicorn or several replicas a cookie issued by one process is rejected by the others (random 401s, unstyled UI, login loops). stemtrace now logs a startup warning when form login runs without a secret (mentioning `WEB_CONCURRENCY` > 1 or gunicorn when detected). In that mode, when a well-formed, unexpired session cookie fails signature verification and the issue time it carries (`iat`, now included in the signed session payload; unverified in this case and used only to classify the rejection, never to grant access) is after the process started (so it most likely came from a sibling worker or replica), it also logs a rate-limited warning (also for rejected WebSocket handshakes) and returns `"reason": "session_secret_mismatch"` in API 401 responses, and HTML routes redirect to the login page with `reason=session_mismatch`, which shows an explanation. Cookies from before a restart, and any badly signed cookie when a secret is configured, get a plain 401 / login redirect.
- Form login: the static UI bundle under `/assets/` (the public open-source JS/CSS build, no data) is now served without a session, so the UI always loads and is styled instead of rendering a blank or unstyled page when a bundle request is rejected. The UI routes, API and WebSocket stay protected, and paths containing `.` or `..` segments (including percent-encoded variants), or an encoded `?` or `#` (`%3F`, `%23`), are rejected with 404 before any exemption applies. Exemptions are decided on the decoded path that routing matches, so e.g. `/api/health/live%3Fx` can't pass as the liveness probe. Paths under `assets/` that aren't real bundle files return 404 instead of the app shell.
- UI: the Logout control is now a button in the header instead of a floating overlay injected by the server, which covered the connection status. On narrow screens the header wraps onto extra rows instead of overflowing, and the status tooltip stays inside the viewport.
- UI: a 401 from the API (missing or expired session) now redirects to the login page with `next` set to the current page, instead of showing "Failed to load ..." errors. A 401 with `reason: "session_secret_mismatch"` shows an explanation instead, since signing in again wouldn't help. Redirects happen at most once per page and not again within a few seconds of the last one, which prevents redirect loops. The WebSocket reconnect now backs off exponentially (3s up to 60s) instead of retrying every 3s forever.
- Task state no longer regresses when events arrive out of order. The sender-side `PENDING` event (from `task_sent`, which fires after publish) could reach the stream after the worker's `STARTED`/`SUCCESS`, and the task showed as `PENDING` again. Node state now comes from the most advanced event, and the result is the same for every arrival order. A terminal state (`SUCCESS`/`FAILURE`/`REVOKED`/`REJECTED`) always beats a non-terminal one, and between terminals the later one wins. Among other states, a newer attempt (higher `retries`) wins, then lifecycle order (`PENDING` < `RECEIVED` < `STARTED`/`RETRY`), with `STARTED` vs `RETRY` in the same attempt decided by time (a redelivered run after a connection-loss retry shows as `STARTED`). A task's events are kept in lifecycle order: the first attempt's `PENDING` then `RECEIVED` first (even when stamped after the task was received or finished), then everything else by timestamp, with a fixed order for identical timestamps. `first_seen`, `last_updated`, `duration_ms`, date filters, sorting and eviction follow that order instead of arrival order. Node aliases from task arguments (`node_alias_from_arguments`) are read from the first event that carries arguments, so a leading `RECEIVED` (which has none) no longer hides them. Task lists, task details and graphs are read from a single snapshot taken under the store lock, so a concurrent event can't make a response mix old and new data (e.g. a `?state=STARTED` filter returning a task that just finished). Naive event timestamps are treated as UTC. Exact duplicate events are ignored and not re-broadcast over the WebSocket.
- UI: the task timeline and event history follow the same order as the API. They used to sort by retry count first, so a failure raised in the worker main process (hard time limit, lost worker), which Celery reports with `retries=0`, appeared before later attempts and produced negative durations. A late-stamped `PENDING` is shown first, with its time clamped to the first run.
- The signal handler no longer raises `TypeError` when Celery cancels a running task (lost broker connection, cold shutdown): Celery sends `task_retry` without a `reason` on that path. The `RETRY` event is now created, with the exception text "cancelled by Celery". During a broker outage it still can't be delivered if stemtrace publishes through that same broker.

## [0.3.6] - 2026-10-10

### Security
- Updated UI bundle dependencies with known advisories (seroval, TanStack Router). The runtime (shipped) dependencies that changed are `@tanstack/react-router` 1.168.1 → 1.170.41, with its `@tanstack/router-core` 1.168.1 → 1.171.34, `@tanstack/history` 1.161.6 → 1.162.4, `@tanstack/react-store`/`@tanstack/store` 0.9.2 → 0.11.2 and `cookie-es` 2.0.0 → 3.1.1; and `seroval`/`seroval-plugins` 1.4.2 → 1.6.8, which fixes GHSA-3rxj-6cgf-8cfw, GHSA-hj76-42vx-jwp4, GHSA-mv8w-475r-vwqw, GHSA-p6vx-979v-rg4c and related denial-of-service advisories. seroval is only used by the router's server-side rendering code, which stemtrace doesn't use, and none of it ends up in the built UI bundle. `@tanstack/react-query` also moved 5.90.16 → 5.94.5 (no advisory). Build-time-only packages (Vite, Rollup, PostCSS and others) were updated too.

### Added
- Health endpoint: `consumer_connected` reports whether the event consumer's last broker read succeeded (`true`), is failing (`false`), or is unknown (`null`). Only the Redis transport reports it: it detects refused or reset connections, connect timeouts and repeated read timeouts. On redis-py < 8, whose default socket timeout is unbounded, a half-open or hung connection may go unnoticed unless the broker URL sets `socket_timeout` or `health_check_interval`. With RabbitMQ it is always `null`.
- UI: "Sprout" brand mark in the header and as the favicon (dashboard and login page). The favicon is an inline SVG data URI that follows the OS light/dark theme, so it needs no extra request, is never redirected by form login, and works under any mount prefix. Brand assets live in `docs/brand/`.

### Changed
- Minimum FastAPI is now 0.112.1 and minimum Starlette 0.38.6: the lowest versions verified by the test suite at release time. FastAPI 0.100–0.104 never worked with current Pydantic (building the app fails with `'FieldInfo' object has no attribute 'in_'`). Starlette is now a declared dependency because stemtrace imports it directly.
- Minimum redis-py is now 4.1.0, the lowest version verified by the test suite at release time; 4.0.x imports `distutils`, which was removed in Python 3.12.
- Docker image: Python dependencies are now installed from the project's lockfile, so each image ships the exact versions audited for that release instead of whatever was newest at build time. Base images are pinned by digest, and the UI is built with Node 22.
- Examples now bind to `127.0.0.1` instead of `0.0.0.0`.
- Docker image: stemtrace now runs from a virtual environment at `/opt/venv` (it's first on `PATH`), and the base image's unused system `pip` is removed. To add packages in a derived image, use `RUN python -m ensurepip && python -m pip install <package>`, which installs into `/opt/venv`.
- Docker image: the built-in `HEALTHCHECK` only checks the HTTP status, so a container whose event consumer is degraded (e.g. the broker is unreachable) still reports healthy. Probe `/stemtrace/api/health` and check its `status` field if you need to alert on that.
- Building from source: the UI build hook now runs `npm ci --ignore-scripts` (or `npm install --ignore-scripts` without a lockfile) instead of `npm install`. Set `STEMTRACE_SKIP_UI_BUILD=1` to skip the UI build entirely; a wheel built that way contains no UI.
- Health endpoint: `status` is now `"degraded"` instead of `"ok"` when the event consumer died unexpectedly, its loop keeps restarting after errors (such as `WRONGTYPE` or `NOPERM`), or `consumer_connected` is `false`. With RabbitMQ only consumer loop failures are detected. A consumer stopped on purpose, or whose event source finished normally (for example `memory://`), is not degraded. The response is still HTTP 200, so status-code-only health checks behave as before; check the `status` field to alert on a degraded consumer. See the README's "Health Endpoint" section.

### Fixed
- Redis transport: the server stopped receiving events after about 5 seconds of quiet when used with redis-py 8. redis-py 8 defaults `socket_timeout` to 5 seconds, the same as the `XREAD` block window, so the first idle read timed out and the consumer thread exited for good. The `XREAD` block window is now half of the client's effective socket timeout (2.5 seconds with redis-py 8 defaults, 5 seconds when the timeout is unbounded), so idle reads return normally on a single connection. This includes clients passed in directly, such as `redis.Redis.from_url(url)`. The client's own timeouts are unchanged, so publishing from tasks never blocks longer than before.
- Redis transport: losing the connection (for example a Redis restart, an unreachable host, or a connect timeout) no longer stops event consumption. The consumer logs one warning with a traceback, then one line per attempt, and retries with exponential backoff (1 s doubling to a 30 s cap, reset on success), resuming from the last stream entry it read. Repeated read timeouts are logged as warnings at most once a minute.
- Server: the event consumer thread restarts with the same backoff after an unexpected error instead of exiting silently, and `stop()` interrupts a pending reconnect wait instead of leaving the thread behind.
- UI: the favicon no longer 404s (the dashboard referenced a non-existent `/vite.svg`).
- UI: JS and CSS assets no longer 404 on FastAPI 0.137+ when stemtrace is installed with per-file symlinks (e.g. `uv pip install --link-mode symlink`). The `/assets` mount now follows symlinks inside the bundle; request paths are still confined to the assets directory.

## [0.3.5] - 2026-10-09

### Security
- UI: fixed a path traversal in the static file handler. Crafted URLs could read any file the server process can access, not just the bundled frontend. When the UI is exposed without authentication (no form login or `auth_dependency`, which is the default for `stemtrace server` and the Docker image), no credentials were needed. All users should upgrade. If you can't upgrade right away, put the UI behind authentication or a reverse proxy that blocks it from untrusted networks.

## [0.3.4] - 2026-06-25

### Fixed
- WebSocket: deny unauthenticated connections during the handshake instead of accepting and then closing them. The accept-then-close path half-opened the connection, so when an unauthenticated client (e.g. a port scanner) aborted mid-handshake, uvicorn's legacy `websockets` implementation ran `close()` on a protocol whose `transfer_data_task` was never set, raising `AttributeError: 'WebSocketProtocol' object has no attribute 'transfer_data_task'`. Closing before `accept()` rejects the handshake (HTTP 403) without opening the protocol object.
- Form login: build the login form action and redirects as relative paths instead of absolute URLs, and derive the session cookie's `Secure` flag from `X-Forwarded-Proto`. Behind a TLS-terminating proxy that doesn't forward the scheme to the ASGI scope (e.g. uvicorn without `--forwarded-allow-ips`), absolute URLs were downgraded to `http://`, so the browser posted the login form over http, the proxy answered `301 -> https`, the POST body was dropped, and login silently failed (with a mixed-content warning). stemtrace now works behind such a proxy without requiring forwarded-headers configuration.

## [0.3.3] - 2026-03-20

### Added
- Node alias from arguments: derive graph node display name from task args/kwargs via `node_alias_from_arguments` config option (thanks @ohadmata for the idea — #46, #48)
- Docker: multi-architecture image builds (amd64 + arm64) (thanks @ohadmata — #51)

### Fixed
- Graph: order task nodes before synthetic container nodes (GROUP/CHORD) for correct rendering (thanks @codyburrito — #52)

## [0.3.2] - 2026-02-28

### Fixed
- Redis transport: normalize `ssl_cert_reqs` URL parameter from `CERT_REQUIRED`/`CERT_NONE`/`CERT_OPTIONAL` to lowercase values expected by redis-py

## [0.3.1] - 2026-01-04

### Added
- UI: optional built-in form login flow for protecting the stemtrace UI

### Changed
- Config: split server broker URL from worker event transport URL (`broker_url` vs `transport_url`)
- API: avoid blocking worker/registry refresh operations
- Docs: clarify optional configuration and retention behavior

### Fixed
- Workers/Registry: refresh via Celery inspect to reduce stale worker/task registry data

## [0.3.0] - 2026-01-04

### Added
- RabbitMQ event transport

### Changed
- Tests: improved RabbitMQ transport coverage and reduced duplication

### Fixed
- UI: harden derived prefix sanitization
- CI/E2E: avoid artifact name conflicts

## [0.2.2] - 2026-01-03

### Added
- README: add release version badge

### Changed
- Packaging/docs: simplify badges and metadata
- README: pin PyPI badge to release version
- Tests: make version assertion dynamic

### Fixed
- Version bump: sync fix

## [0.2.1] - 2026-01-03

### Added
- Task Registry now includes task definition metadata from workers (module, signature, docstring, bound)

### Changed
- README: updated PyPI badge styling
- CI: upload Codecov coverage on `main`

## [0.2.0] - 2026-01-03

### Added
- Workers API endpoints: `GET /api/workers` and `GET /api/workers/{hostname}`
- Worker lifecycle tracking via `worker_ready` / `worker_shutdown` events to maintain a live worker registry
- Workers UI page for inspecting online/offline workers and their registered tasks
- Task Registry now shows which workers registered each task (`registered_by`)

## [0.1.1] - 2025-12-31

### Added
- Python 3.14 support in CI and project classifiers
- Mock-based E2E testing mode (no Docker required for local dev)
- Codecov integration for coverage reporting

### Changed
- Upgraded React and React-DOM to v19
- Upgraded @tanstack/react-query to latest
- Updated GitHub Actions to v6 (checkout, setup-python, setup-node, upload/download-artifact)
- Improved README with clearer Quick Start and FastAPI embedding docs

## [0.1.0] - 2025-12-27

### Added
- **Core domain models**: `TaskEvent`, `TaskState`, `TaskNode`, `TaskGraph`
- **Protocol definitions**: `EventTransport`, `TaskRepository`, `AsyncEventConsumer`
- **Event transports**: Redis Streams (`RedisTransport`), in-memory (`MemoryTransport`)
- **Celery signal integration**: Automatic event capture via `stemtrace.init_worker(app)`
- **Server components**:
  - `GraphStore` — Thread-safe in-memory graph storage with LRU eviction
  - `EventConsumer` / `AsyncEventConsumer` — Background event processing
  - `WebSocketManager` — Real-time event broadcasting
- **REST API**: `/api/tasks`, `/api/graphs`, `/api/health` endpoints
- **FastAPI integration**:
  - `StemtraceExtension` — Full extension with lifespan management
  - `create_router()` — Minimal router for custom setups
  - Auth helpers: `require_basic_auth`, `require_api_key`, `no_auth`
- **React UI**: Task list, graph visualization (react-flow), timeline view
- **CLI commands**: `stemtrace server`, `stemtrace consume`
- **Docker support**: Multi-stage Dockerfile, docker-compose.yml for local dev
- **E2E test suite**: Docker API tests + Playwright browser tests
- **Comprehensive test suite**: 350+ Python tests, 90%+ coverage

[unreleased]: https://github.com/iansokolskyi/stemtrace/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.10...v0.4.0
[0.3.10]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.9...v0.3.10
[0.3.9]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.8...v0.3.9
[0.3.8]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/iansokolskyi/stemtrace/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/iansokolskyi/stemtrace/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/iansokolskyi/stemtrace/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/iansokolskyi/stemtrace/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/iansokolskyi/stemtrace/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/iansokolskyi/stemtrace/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/iansokolskyi/stemtrace/releases/tag/v0.1.0
