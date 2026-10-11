"""Redis transport using Redis Streams."""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from typing_extensions import Self

from stemtrace.core.events import TaskEvent, WorkerEvent
from stemtrace.transports.breaker import PublishBreaker

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterator

    from redis import Redis

logger = logging.getLogger(__name__)

# Type alias for all events that can be consumed from the stream
StreamEvent = TaskEvent | WorkerEvent

# Longest a single XREAD blocks waiting for new entries. The actual window is
# shortened to half the consume client's effective socket timeout when that is
# bounded (redis-py 8 defaults socket_timeout to 5s), so an idle read returns
# before the socket times out. Publishing uses a separate client (see below),
# so the consume client keeps redis-py's own timeouts.
XREAD_BLOCK_MS = 5000

# Consecutive XREAD read timeouts tolerated silently before warning.
READ_TIMEOUT_WARN_AFTER = 3

# Minimum seconds between repeated read-timeout warnings.
READ_TIMEOUT_WARN_INTERVAL = 60.0

# Reconnect backoff after connection failures: doubles per failure, resets on success.
RECONNECT_INITIAL_BACKOFF = 1.0
RECONNECT_MAX_BACKOFF = 30.0

# Publishing runs in the Celery task's thread, so from_url() gives it its own
# client whose connect (per address the host name resolves to) and socket
# reads/writes time out after these many seconds (redis-py 8.1 defaults both
# to 5s), with no redis-py retries: a silent or blackholed Redis costs a few
# bounded attempts, not 5s per event. Host name resolution (getaddrinfo) is
# not covered by these timeouts. Not lower: the deadlines are wall-clock, and
# a thread that waits for the GIL while another holds it in a long C call
# (measured: 1.3s) can miss a 1s TLS handshake deadline on a healthy Redis.
PUBLISH_CONNECT_TIMEOUT = 2.0
PUBLISH_SOCKET_TIMEOUT = 2.0

# Seconds after which a breaker probe that never reported back is presumed
# lost. Far longer than any real attempt.
PUBLISH_PROBE_TIMEOUT = 60.0

# PUBLISH_FAILURES_TO_OPEN failed publishes in a row (timeouts included) open
# the publish circuit breaker. Until then, a failure loses only its own event:
# a single timeout may come from a healthy Redis (e.g. the publishing thread
# waited for the GIL past its socket deadline, or Redis stalled briefly).
PUBLISH_FAILURES_TO_OPEN = 3

# While the breaker is open, events are dropped without contacting Redis for
# a cooldown that doubles per failed retry (RECONNECT_INITIAL_BACKOFF up to
# RECONNECT_MAX_BACKOFF) and resets once a publish succeeds. At
# most four publish WARNING lines per interval (see PublishBreaker).
PUBLISH_FAILURE_LOG_INTERVAL = 60.0

# URL options that would override the publish client's timeouts (redis-py
# lets querystring options win over keyword arguments in from_url()).
_PUBLISH_TIMEOUT_QUERY_PARAMS = ("socket_timeout", "socket_connect_timeout")

# Mapping from CERT_* constants (used in URLs) to lowercase values expected by redis-py
_SSL_CERT_REQS_MAP = {
    "CERT_NONE": "none",
    "CERT_OPTIONAL": "optional",
    "CERT_REQUIRED": "required",
}


def _normalize_redis_ssl_params(url: str) -> str:
    """Normalize ssl_cert_reqs in a Redis URL from CERT_* to lowercase.

    redis-py expects lowercase values (none/optional/required) but URLs
    commonly use the Python ssl module constants (CERT_NONE/CERT_REQUIRED).
    """
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    parsed = urlparse(url)
    if not parsed.query:
        return url
    params = parse_qs(parsed.query)
    if "ssl_cert_reqs" not in params:
        return url
    raw = params["ssl_cert_reqs"][0]
    normalized = _SSL_CERT_REQS_MAP.get(raw, raw)
    params["ssl_cert_reqs"] = [normalized]
    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _without_query_params(url: str, names: tuple[str, ...]) -> str:
    """Return ``url`` with the given querystring options removed."""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parsed.query) if k not in names]
    return urlunparse(parsed._replace(query=urlencode(kept)))


# Whether the current publish attempt has written its XADD to the socket. A
# timeout or dropped connection after that may still leave the entry in the
# stream (Redis applies the command, the reply is lost), so the event counts
# as unconfirmed rather than dropped. A ContextVar, so threads and greenlets
# publishing at once each see their own attempt.
_XADD_SENT: ContextVar[bool] = ContextVar("stemtrace_redis_xadd_sent", default=False)


def _with_xadd_tracking(connection_class: type[Any]) -> type[Any]:
    """Subclass a redis-py connection class to set _XADD_SENT once XADD is written.

    redis-py 8.1 Redis._send_command_parse_response() calls
    ``connection.send_command(command_name, *args)`` and only then reads the
    reply; send_command() returns once the whole command was written (the
    handshake commands sent while connecting go through it too, hence the
    name check).
    """

    def send_command(self: Any, *args: Any, **kwargs: Any) -> None:
        """Send a command; note an XADD once it was written in full."""
        connection_class.send_command(self, *args, **kwargs)
        if args and args[0] == "XADD":
            _XADD_SENT.set(True)

    return type(
        f"Publish{connection_class.__name__}",
        (connection_class,),
        {"send_command": send_command},
    )


def _publish_client_from_url(url: str) -> Redis[Any]:
    """Build the client publish() uses: short timeouts, no retries.

    ``retry`` is pinned to zero retries because a ``retry_on_timeout`` URL
    option would otherwise add one (redis-py 8.1 AbstractConnection.__init__
    builds ``Retry(NoBackoff(), 1)`` for it), and timeouts in the URL are
    dropped because they would win over the keyword arguments.

    RedisTransport builds one of these per process (see ``publish_client``).
    The client doesn't own its pool (``auto_close_connection_pool`` is
    False), so when a forked child drops the parent's client, garbage
    collection never closes the inherited pool: closing it would run
    ConnectionPool.reset(), which takes the pool's lock without a timeout
    (redis-py 8.1) and hangs if a parent thread held it at fork.
    """
    from redis import ConnectionPool
    from redis import Redis as RedisClient
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    pool = ConnectionPool.from_url(
        _without_query_params(url, _PUBLISH_TIMEOUT_QUERY_PARAMS),
        decode_responses=False,
        socket_connect_timeout=PUBLISH_CONNECT_TIMEOUT,
        socket_timeout=PUBLISH_SOCKET_TIMEOUT,
        retry=Retry(NoBackoff(), 0),
    )
    # from_url() picked the class for the scheme (TCP, TLS or Unix socket).
    pool.connection_class = _with_xadd_tracking(pool.connection_class)
    return RedisClient(connection_pool=pool)


def _reply_lost(exc: Exception) -> bool:
    """Whether a publish error leaves it unknown if Redis applied the command.

    A timeout or a broken connection while waiting for the reply does; an
    error reply (e.g. OOM, WRONGTYPE) means Redis rejected the command.
    """
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    return isinstance(exc, RedisConnectionError | RedisTimeoutError)


def _is_connect_timeout(exc: Exception) -> bool:
    """Whether a redis-py TimeoutError came from connecting rather than reading."""
    return str(exc).startswith("Timeout connecting")


_UNKNOWN = object()


def _class_default_socket_timeout(connection_class: object) -> object:
    """Return the socket_timeout default declared by a redis-py connection class.

    redis-py 8 declares 5 seconds, earlier versions None. Subclasses such as
    SSLConnection take ``**kwargs``, so walk the MRO to the class declaring it.
    """
    for klass in getattr(connection_class, "__mro__", ()):
        init = klass.__dict__.get("__init__")
        if init is None:
            continue
        try:
            parameter = inspect.signature(init).parameters.get("socket_timeout")
        except (TypeError, ValueError):
            continue
        if parameter is not None and parameter.default is not inspect.Parameter.empty:
            return parameter.default
    return _UNKNOWN


def effective_socket_timeout(client: Redis[Any]) -> float | None:
    """Socket timeout (seconds) the client's connections will actually use.

    An explicit ``socket_timeout`` in the pool's connection kwargs wins (None
    means unbounded). Otherwise the connection class default applies, which
    ``Redis.from_url()`` relies on: 5 seconds on redis-py 8, None before.
    Unknown or unexpected values are treated as unbounded.
    """
    pool = getattr(client, "connection_pool", None)
    kwargs = getattr(pool, "connection_kwargs", None)
    if isinstance(kwargs, dict) and "socket_timeout" in kwargs:
        timeout: object = kwargs["socket_timeout"]
    else:
        timeout = _class_default_socket_timeout(getattr(pool, "connection_class", None))
    if (
        isinstance(timeout, int | float)
        and not isinstance(timeout, bool)
        and timeout > 0
    ):
        return float(timeout)
    return None


def _xread_block_ms(client: Redis[Any]) -> int:
    """Pick an XREAD block window that ends before the socket read times out."""
    timeout = effective_socket_timeout(client)
    if timeout is None:
        return XREAD_BLOCK_MS
    return max(1, min(XREAD_BLOCK_MS, int(timeout * 1000 / 2)))


@dataclass
class _ReadHealth:
    """Failure bookkeeping for one consume() iterator.

    Kept per iterator, not on the transport, so a new consume() call starts
    with a clean slate (like ``connected``, which it resets to None).
    """

    # Consecutive failures to reach Redis (connection errors, connect timeouts).
    failures: int = 0
    # Consecutive XREAD read timeouts.
    read_timeouts: int = 0
    # Delay before the next reconnect attempt. Read when consume() starts,
    # not at import, so a patched constant applies to the first wait too.
    backoff: float = field(default_factory=lambda: RECONNECT_INITIAL_BACKOFF)
    # Monotonic time of the last read-timeout warning, for rate limiting.
    last_timeout_warning: float | None = None

    def reset(self) -> None:
        """Forget all failures after a read that reached Redis."""
        self.failures = 0
        self.read_timeouts = 0
        self.backoff = RECONNECT_INITIAL_BACKOFF
        self.last_timeout_warning = None


class RedisTransport:
    """Redis Streams-based event transport (XADD/XREAD)."""

    def __init__(
        self,
        client: Redis[Any],
        prefix: str,
        ttl: int,
        *,
        publish_client_factory: Callable[[], Redis[Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize Redis transport with client and stream configuration.

        Args:
            client: Redis client used for XREAD (and XADD unless
                ``publish_client_factory`` is given).
            prefix: Key prefix; the stream key is ``<prefix>:events``.
            ttl: Event retention hint, also used to size the stream.
            publish_client_factory: Builds the Redis client used for XADD,
                once per process (see ``publish_client``). Without it, XADD
                uses ``client``; from_url() passes one with short timeouts.
            sleep: Sleep function for reconnect backoff (injectable for tests).
            monotonic: Clock used to rate-limit warnings and time publish
                attempts for the circuit breaker (injectable for tests).
        """
        self._client = client
        self._publish_client_factory = publish_client_factory
        # (pid, client) of the publish client built in the current process.
        self._process_publish_client: tuple[int, Redis[Any]] | None = None
        self._publish_breaker = PublishBreaker(
            "Redis",
            logger=logger,
            initial_cooldown=RECONNECT_INITIAL_BACKOFF,
            max_cooldown=RECONNECT_MAX_BACKOFF,
            log_interval=PUBLISH_FAILURE_LOG_INTERVAL,
            probe_timeout=PUBLISH_PROBE_TIMEOUT,
            failures_to_open=PUBLISH_FAILURES_TO_OPEN,
            monotonic=monotonic,
        )
        self._ttl = ttl
        self._stream_key = f"{prefix}:events"
        self._maxlen = max(ttl, 10000)
        self._sleep = sleep
        self._monotonic = monotonic
        self._block_ms = _xread_block_ms(client)
        self._last_id = "0"
        self._connected: bool | None = None
        self._stop_event: threading.Event | None = None

    @property
    def connected(self) -> bool | None:
        """Whether the last XREAD reached Redis.

        None until the first read of the current consume() completes, True
        after a successful read, False while reads are failing (connection
        errors or repeated timeouts).
        """
        return self._connected

    def bind_stop_event(self, event: threading.Event) -> None:
        """Stop consume() and interrupt its reconnect backoff when ``event`` is set."""
        self._stop_event = event

    @property
    def client(self) -> Redis[Any]:
        """The Redis client consume() reads with."""
        return self._client

    @property
    def publish_client(self) -> Redis[Any]:
        """The Redis client publish() writes with in this process.

        With a ``publish_client_factory``, the client is built on first use
        and again after a fork (a new PID), like the publish breaker, so a
        prefork child never uses a connection pool or lock inherited from
        its parent. Two threads publishing for the first time at once may
        both build one; the extra client is dropped, which is harmless.
        """
        if self._publish_client_factory is None:
            return self._client
        current = self._process_publish_client
        pid = os.getpid()
        if current is None or current[0] != pid:
            current = (pid, self._publish_client_factory())
            self._process_publish_client = current
        return current[1]

    @property
    def stream_key(self) -> str:
        """The stream key."""
        return self._stream_key

    @property
    def ttl(self) -> int:
        """TTL in seconds."""
        return self._ttl

    def publish(self, event: StreamEvent) -> None:
        """Publish an event to the Redis stream.

        Fire-and-forget: never raises, and events that can't be published are
        dropped, not queued. With a transport from from_url(), an attempt
        takes at most PUBLISH_CONNECT_TIMEOUT per address to connect plus
        PUBLISH_SOCKET_TIMEOUT per socket read/write, without retries, plus
        however long resolving the host name takes (not bounded here).

        Any reply slower than PUBLISH_SOCKET_TIMEOUT is a failure. A failure
        after the XADD was written (a timeout or lost connection) counts the
        event as sent but unconfirmed, since Redis may still have applied it;
        other failures count it as dropped. PUBLISH_FAILURES_TO_OPEN failures
        in a row open a circuit breaker; until then a failure loses only its
        own event. While open, events are dropped without contacting Redis
        for a cooldown (1s, doubling per failed retry up to 30s, reset by a
        successful publish), so an outage costs tasks
        PUBLISH_FAILURES_TO_OPEN bounded attempts, then about one per
        cooldown, instead of one per event. Per PUBLISH_FAILURE_LOG_INTERVAL,
        at most four WARNING lines are written, each with the number of
        events lost since the previous line; each event dropped while the
        breaker is open also gets a DEBUG line.

        Args:
            event: Event to publish (TaskEvent or WorkerEvent).
        """
        breaker = self._publish_breaker
        ticket = breaker.allow()
        if ticket is None:
            logger.debug(
                "Redis publishing paused after failures; dropped event %s",
                self._event_identifier(event),
            )
            return
        sent = _XADD_SENT.set(False)
        try:
            self.publish_client.xadd(
                self._stream_key,
                {"data": event.model_dump_json()},
                maxlen=self._maxlen,
                approximate=True,
            )
        except Exception as exc:  # noqa: BLE001  fire-and-forget: any redis-py error feeds the breaker, never the task
            breaker.record_failure(
                ticket,
                self._event_identifier(event),
                exc,
                unconfirmed=_XADD_SENT.get() and _reply_lost(exc),
            )
        else:
            breaker.record_success(ticket)
        finally:
            _XADD_SENT.reset(sent)

    @staticmethod
    def _event_identifier(event: StreamEvent) -> str:
        """Create a stable identifier for logging."""
        if isinstance(event, TaskEvent):
            return event.task_id
        return f"{event.hostname}:{event.pid}:{event.event_type.value}"

    def consume(self, last_id: str | None = None) -> Iterator[StreamEvent]:
        """Blocking iterator that yields events as they arrive.

        Detects event type from JSON and yields appropriate model
        (TaskEvent or WorkerEvent).

        Connection errors and connect timeouts never end the iterator: the
        transport logs a warning, backs off exponentially (capped at
        RECONNECT_MAX_BACKOFF seconds, reset after a successful read) and
        resumes from the last stream id it saw. An occasional read timeout is
        retried immediately; repeated read timeouts are logged as warnings.
        Other Redis errors (e.g. WRONGTYPE, NOPERM) propagate to the caller.
        The iterator ends once a stop event bound with bind_stop_event() is
        set, and that event also interrupts the backoff wait.

        Args:
            last_id: Stream id to read after. Defaults to the last id this
                transport consumed ("0", the start of the stream, initially).
        """
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import RedisError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        if last_id is not None:
            self._last_id = last_id
        # Unknown until this iterator's first read completes.
        self._connected = None
        health = _ReadHealth()

        while not self._stopping():
            try:
                results = self._client.xread(
                    {self._stream_key: self._last_id},
                    block=self._block_ms,
                    count=100,
                )
            except RedisTimeoutError as exc:
                if _is_connect_timeout(exc):
                    self._on_unreachable(health, exc)
                else:
                    self._on_read_timeout(health)
                continue
            except RedisConnectionError as exc:
                self._on_unreachable(health, exc)
                continue
            except RedisError:
                # Not a connectivity problem (e.g. WRONGTYPE, NOPERM): report
                # reads as failing and let the caller decide how to recover.
                self._connected = False
                raise

            self._on_read(health)
            yield from self._events_from(results)

    def _stopping(self) -> bool:
        """Whether a bound stop event has been set."""
        return self._stop_event is not None and self._stop_event.is_set()

    def _on_unreachable(self, health: _ReadHealth, exc: Exception) -> None:
        """Count a failure to reach Redis, then back off before the next read.

        A connection failure also ends any read-timeout streak: the timeouts
        are counted again from zero once a connection is back.
        """
        health.failures += 1
        health.read_timeouts = 0
        health.backoff = self._back_off(health.failures, health.backoff, exc)

    def _on_read_timeout(self, health: _ReadHealth) -> None:
        """Retry a read that timed out at once, warning only about a streak.

        The first READ_TIMEOUT_WARN_AFTER - 1 timeouts in a row are logged at
        debug level. From then on reads count as failing, with at most one
        warning per READ_TIMEOUT_WARN_INTERVAL. No backoff: Redis answered
        the connection, the read just outlasted the socket timeout.
        """
        health.read_timeouts += 1
        if health.read_timeouts < READ_TIMEOUT_WARN_AFTER:
            logger.debug("XREAD on %s timed out; retrying", self._stream_key)
            return
        self._connected = False
        now = self._monotonic()
        last = health.last_timeout_warning
        if last is not None and now - last < READ_TIMEOUT_WARN_INTERVAL:
            return
        health.last_timeout_warning = now
        logger.warning(
            "XREAD on %s timed out %d times in a row; check the "
            "network and the socket_timeout setting",
            self._stream_key,
            health.read_timeouts,
        )

    def _on_read(self, health: _ReadHealth) -> None:
        """Record a read that reached Redis: the outage, if any, is over."""
        if health.failures:
            logger.info(
                "Redis consumer on %s reconnected after %d failure(s)",
                self._stream_key,
                health.failures,
            )
        health.reset()
        self._connected = True

    def _events_from(self, results: Any) -> Iterator[StreamEvent]:
        """Yield the events of one XREAD reply, skipping unparseable entries.

        The last id advances past every entry, including ones without data or
        that fail to parse, so they are never read again.
        """
        if not results:
            return
        for _stream_name, messages in results:
            for message_id, fields in messages:
                self._last_id = (
                    message_id.decode() if isinstance(message_id, bytes) else message_id
                )
                event = self._event_from_fields(fields)
                if event is not None:
                    yield event

    def _event_from_fields(self, fields: Any) -> StreamEvent | None:
        """Parse an entry's ``data`` field; None when missing or malformed."""
        data = fields.get(b"data") or fields.get("data")
        if not data:
            return None
        data_str = data.decode() if isinstance(data, bytes) else data
        try:
            return self._parse_event(data_str)
        except ValueError:  # includes JSONDecodeError and pydantic's ValidationError
            logger.warning(
                "Failed to parse event from Redis stream %s at id %s",
                self._stream_key,
                self._last_id,
                exc_info=True,
            )
            return None

    def _back_off(self, failures: int, backoff: float, exc: Exception) -> float:
        """Log a consume failure, sleep, and return the next backoff delay."""
        self._connected = False
        if failures == 1:
            logger.warning(
                "Redis consumer on %s cannot reach Redis; retrying in %.1fs",
                self._stream_key,
                backoff,
                exc_info=True,
            )
        else:
            logger.warning(
                "Redis consumer on %s still failing (attempt %d): %s; "
                "retrying in %.1fs",
                self._stream_key,
                failures,
                exc,
                backoff,
            )
        if self._stop_event is not None:
            self._stop_event.wait(backoff)
        else:
            self._sleep(backoff)
        return min(backoff * 2, RECONNECT_MAX_BACKOFF)

    def _parse_event(self, data_str: str) -> StreamEvent:
        """Parse JSON into appropriate event type."""
        # Peek at JSON to determine event type
        parsed = json.loads(data_str)
        if not isinstance(parsed, dict):
            raise ValueError("Event payload is not a JSON object")
        if "event_type" in parsed:
            # WorkerEvent has event_type field
            return WorkerEvent.model_validate(parsed)
        if "task_id" in parsed:
            # TaskEvent has task_id field
            return TaskEvent.model_validate(parsed)
        raise ValueError("Unknown event payload: missing event_type and task_id")

    @classmethod
    def from_url(cls, url: str, prefix: str = "stemtrace", ttl: int = 86400) -> Self:
        """Create transport from Redis URL.

        The consume client keeps redis-py's own timeout defaults; consume()
        adapts its XREAD block window to them. publish() gets a separate
        client with PUBLISH_CONNECT_TIMEOUT/PUBLISH_SOCKET_TIMEOUT and no
        retries, overriding any timeouts given in the URL. Neither connects
        before its first command.
        """
        from redis import Redis as RedisClient

        url = _normalize_redis_ssl_params(url)
        return cls(
            client=RedisClient.from_url(url, decode_responses=False),
            prefix=prefix,
            ttl=ttl,
            publish_client_factory=functools.partial(_publish_client_from_url, url),
        )
