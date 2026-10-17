# Brokers

stemtrace sends its events through the broker you already run. This page covers how workers
publish to each broker, what happens when the broker is slow or down, and the known limitations.
What the server can rebuild after a restart is in [Event retention](retention.md).

## Supported brokers

| Broker | URL Scheme | Status |
|--------|------------|--------|
| Redis | `redis://`, `rediss://` | Supported |
| RabbitMQ | `amqp://`, `amqps://`, `pyamqp://` | Supported |

To keep durable history with a RabbitMQ broker, send stemtrace's events to Redis; see
[Event retention](retention.md#durable-history-across-restarts).

## Redis (Streams)

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

## RabbitMQ (fanout + per-consumer queue)

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
