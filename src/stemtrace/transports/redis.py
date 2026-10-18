"""Redis transport using Redis Streams."""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import socket
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from typing_extensions import Self

from stemtrace.core.events import TaskEvent, WorkerEvent
from stemtrace.core.exceptions import is_task_interrupt
from stemtrace.transports.breaker import PublishBreaker
from stemtrace.transports.resolver import PUBLISH_RESOLVER

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

# Least time one idle read (an empty reply or a read timeout) takes in
# consume(); a faster one is followed by a wait for the rest. The block window
# is half the socket timeout (see _xread_block_ms), so a tiny ``socket_timeout``
# in the URL (or a Redis slower than it) would otherwise make idle reads
# return at once and loop back, one round trip after another (measured: 110
# reads/s and a quarter of a core for ``socket_timeout=0.001``). With
# redis-py's 5s default the block window is 2.5s and this never applies.
MIN_IDLE_READ_SECONDS = 0.25

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
# bounded attempts, not 5s per event. Host name resolution waits at most the
# connect timeout too (see PUBLISH_RESOLVER). Not lower: the deadlines are wall-clock, and
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


def _query_items(url: str) -> tuple[str, list[str]]:
    """Split a URL into the part before ``?`` and its raw ``&``-separated items.

    The URL is never re-assembled by urllib: urlunparse() turns
    ``unix:///path`` into ``unix:/path``, which redis-py 4.5+ rejects.
    """
    base, _, query = url.partition("?")
    return base, [item for item in query.split("&") if item]


def _query_key(item: str) -> str:
    """The (unquoted) option name of a raw query item such as ``db=1``."""
    from urllib.parse import unquote_plus

    return unquote_plus(item.partition("=")[0])


def _with_query_items(base: str, items: list[str]) -> str:
    """Re-join a URL split by _query_items()."""
    return f"{base}?{'&'.join(items)}" if items else base


def _normalize_redis_ssl_params(url: str) -> str:
    """Normalize ssl_cert_reqs in a Redis URL from CERT_* to lowercase.

    redis-py expects lowercase values (none/optional/required) but URLs
    commonly use the Python ssl module constants (CERT_NONE/CERT_REQUIRED).
    Everything else in the URL is kept as written.
    """
    from urllib.parse import unquote_plus

    base, items = _query_items(url)
    for index, item in enumerate(items):
        if _query_key(item) == "ssl_cert_reqs":
            raw = unquote_plus(item.partition("=")[2])
            items[index] = f"ssl_cert_reqs={_SSL_CERT_REQS_MAP.get(raw, raw)}"
    return _with_query_items(base, items) if "?" in url else url


def _without_query_params(url: str, names: tuple[str, ...]) -> str:
    """Return ``url`` with the given querystring options removed."""
    base, items = _query_items(url)
    if "?" not in url:
        return url
    return _with_query_items(base, [i for i in items if _query_key(i) not in names])


# Whether the current publish attempt has written its XADD to the socket. A
# timeout or dropped connection after that may still leave the entry in the
# stream (Redis applies the command, the reply is lost), so the event counts
# as unconfirmed rather than dropped. A ContextVar, so threads and greenlets
# publishing at once each see their own attempt.
_XADD_SENT: ContextVar[bool] = ContextVar("stemtrace_redis_xadd_sent", default=False)


@functools.cache
def _resolved_connect_mixin() -> type[Any]:
    """A redis-py Connection whose TCP connect resolves via PUBLISH_RESOLVER.

    Only the TCP socket changes: TLS still sends SNI for, and verifies the
    certificate against, the host name in the URL.
    """
    from redis.connection import Connection

    class ResolvedConnect(Connection):
        """redis-py TCP connection that takes its addresses from PUBLISH_RESOLVER."""

        def _resolved_addresses(self) -> list[tuple[Any, ...]]:
            """Addresses for this connection's host, from the bounded resolver."""
            return PUBLISH_RESOLVER.resolve(
                self.host,
                self.port,
                self.socket_type,
                socket.SOCK_STREAM,
                timeout=self.socket_connect_timeout or PUBLISH_CONNECT_TIMEOUT,
            )

        def _connect_failed(self) -> None:
            """Refresh the host's cached addresses: it may have moved."""
            PUBLISH_RESOLVER.invalidate(
                self.host, self.port, self.socket_type, socket.SOCK_STREAM
            )

        # A copy of redis-py 8.1 Connection._connect() (same socket options),
        # which calls getaddrinfo() unbounded; SSLConnection._connect() wraps
        # the returned socket with server_hostname=self.host.
        def _connect(self) -> socket.socket:
            """Connect to the first resolved address that accepts."""
            err: OSError | None = None
            for family, socktype, proto, _, address in self._resolved_addresses():
                sock = None
                try:
                    sock = socket.socket(family, socktype, proto)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    if self.socket_keepalive:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                        # Mapping[int, int | bytes] at runtime; redis-py's
                        # annotations are looser (they include object).
                        options: Any = self.socket_keepalive_options
                        for k, v in options.items():
                            sock.setsockopt(socket.IPPROTO_TCP, k, v)
                    sock.settimeout(self.socket_connect_timeout)
                    sock.connect(address)
                    sock.settimeout(self.socket_timeout)
                except OSError as exc:
                    err = exc
                    if sock is not None:
                        sock.close()
                else:
                    return sock
            self._connect_failed()
            raise err if err is not None else OSError("No address to connect to")

    return ResolvedConnect


def _publish_connection_class(connection_class: type[Any]) -> type[Any]:
    """Subclass the URL's redis-py connection class for publishing.

    TCP and TLS connections resolve the host through PUBLISH_RESOLVER, every
    connection accepts the pool's keywords on all supported redis-py versions,
    and every connection sets _XADD_SENT once an XADD is written in full.
    """

    def send_command(self: Any, *args: Any, **kwargs: Any) -> None:
        """Send a command; note an XADD once it was written in full."""
        # redis-py 8.1 _send_command_parse_response() reads the reply only after
        # send_command() wrote it all; handshake commands pass here too.
        connection_class.send_command(self, *args, **kwargs)
        if args and args[0] == "XADD":
            _XADD_SENT.set(True)

    return type(
        f"Publish{connection_class.__name__}",
        _publish_bases(connection_class),
        {
            "__init__": _keyword_compatible_init(connection_class),
            "send_command": send_command,
        },
    )


def _publish_bases(connection_class: type[Any]) -> tuple[type[Any], ...]:
    """Base classes for the publish subclass: add the resolver hook for TCP/TLS."""
    from redis.connection import Connection, UnixDomainSocketConnection

    # A Unix socket class (a Connection subclass in redis-py 4.x) has no host
    # to resolve, and a class that isn't a Connection has no _connect() to hook.
    resolves_host = issubclass(connection_class, Connection) and not issubclass(
        connection_class, UnixDomainSocketConnection
    )
    bases: list[type[Any]] = []
    if connection_class is not Connection:
        bases.append(connection_class)
    if resolves_host:
        # E.g. SSLConnection: its _connect() calls super()._connect(), which
        # the MRO (SSLConnection, ResolvedConnect, Connection) sends here.
        bases.append(_resolved_connect_mixin())
    return tuple(bases)


def _keyword_compatible_init(connection_class: type[Any]) -> Callable[..., None]:
    """An __init__ that passes on only the keywords the base class takes.

    The publish timeouts are set afterwards whether or not the class takes them.
    Other options the class doesn't take (e.g. a typo in the URL) are logged
    once, not silently lost.
    """
    accepted = _accepted_init_kwargs(connection_class)
    warned = False

    def __init__(self: Any, **kwargs: Any) -> None:
        """Initialize the base connection, then set the publish timeouts."""
        nonlocal warned
        kwargs_for_base = kwargs
        if accepted is not None:
            kwargs_for_base = {k: v for k, v in kwargs.items() if k in accepted}
            dropped = set(kwargs) - accepted - set(_TIMEOUTS)
            if dropped and not warned:
                warned = True
                logger.warning(
                    "Redis URL option(s) not accepted by redis-py's %s (unknown "
                    "or unsupported option), ignored for publishing: %s",
                    connection_class.__name__,
                    ", ".join(sorted(dropped)),
                )
        connection_class.__init__(self, **kwargs_for_base)
        # redis-py 4.x UnixDomainSocketConnection takes no socket_connect_timeout
        # and 5.0.0-5.0.6 drop socket_timeout for Unix sockets.
        for name in _TIMEOUTS:
            if name in kwargs:
                setattr(self, name, kwargs[name])

    return __init__


# Set on publish connections after __init__ whether or not the class takes them.
_TIMEOUTS = ("socket_timeout", "socket_connect_timeout")


def _accepted_init_kwargs(connection_class: type[Any]) -> frozenset[str] | None:
    """Keyword arguments a class's __init__ chain accepts; None if any.

    Follows ``**kwargs`` up the MRO (e.g. UnixDomainSocketConnection forwards
    them to AbstractConnection in redis-py 4.5) until an __init__ without it.
    """
    names: set[str] = set()
    # Without object (always last): reaching it means ``**kwargs`` all the way
    # up, so any keyword is accepted.
    for klass in connection_class.__mro__[:-1]:
        init = klass.__dict__.get("__init__")
        if init is None:
            continue
        parameters = inspect.signature(init).parameters.values()
        names.update(
            p.name
            for p in parameters
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        )
        if not any(p.kind is p.VAR_KEYWORD for p in parameters):
            return frozenset(names)
    return None


def _publish_client_from_url(url: str) -> Redis[Any]:
    """Build the client publish() uses: short timeouts, no retries.

    Timeouts in the URL are dropped because they would win over the keyword
    arguments. RedisTransport builds one of these per process.
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
        # A retry_on_timeout URL option would add one retry (redis-py 8.1
        # AbstractConnection.__init__ builds Retry(NoBackoff(), 1) for it).
        retry=Retry(NoBackoff(), 0),
    )
    # from_url() picked the class for the scheme (TCP, TLS or Unix socket).
    pool.connection_class = _publish_connection_class(pool.connection_class)
    # Not owning the pool, a client a forked child drops never closes it:
    # redis-py 8.1 ConnectionPool.reset() takes the pool lock with no timeout,
    # which hangs if a parent thread held it at fork.
    return RedisClient(connection_pool=pool)


def _reply_lost(exc: Exception) -> bool:
    """Whether a publish error leaves it unknown if Redis applied the command.

    Only transport failures do: a socket timeout, or the connection closing or
    breaking while the reply was awaited. An error Redis replied with means it
    rejected the command, even when redis-py raises it as a ConnectionError.
    """
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    if isinstance(exc, RedisTimeoutError):
        return True
    # redis-py 8.1 raises some server replies as ConnectionError or subclasses
    # (-LOADING, -NOAUTH, max clients; _parsers/base.py EXCEPTION_CLASSES):
    # they carry the server's message, transport errors the fixed ones below.
    if type(exc) is not RedisConnectionError:
        return False
    message = str(exc)
    return message == _SERVER_CLOSED_CONNECTION or message.startswith(
        _READ_FAILED_PREFIX
    )


# redis-py 8.1's messages for a reply lost in transit (connection.py
# read_response(), _parsers/socket.py SERVER_CLOSED_CONNECTION_ERROR).
_SERVER_CLOSED_CONNECTION = "Connection closed by server."
_READ_FAILED_PREFIX = "Error while reading from "


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
            monotonic: Clock used to rate-limit warnings and to time the
                publish circuit breaker's cooldowns (injectable for tests).
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

        With a ``publish_client_factory``, the client is built on first use and
        again after a fork, so a prefork child never uses its parent's pool or
        lock. Two threads racing here may both build one; the extra is dropped.
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

        Fire-and-forget: events that can't be published are dropped, not queued.
        Only Celery control flow (e.g. ``SoftTimeLimitExceeded``) propagates. A
        failure after the XADD was written counts as sent but unconfirmed, and
        repeated failures open a circuit breaker (details in docs/brokers.md).

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
        # Fire-and-forget: any redis-py error feeds the breaker, never the task.
        except Exception as exc:
            if is_task_interrupt(exc):
                # A soft time limit landed mid-publish: the task's, not a
                # broker fault. redis-py drops the connection on it (it
                # disconnects on BaseException), so the next publish starts
                # clean; the breaker slot is freed without a failure.
                breaker.record_interrupted(ticket)
                raise
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

        Connection errors and timeouts never end it: it backs off exponentially
        (up to RECONNECT_MAX_BACKOFF) and resumes from the last stream id it saw.
        Other Redis errors (e.g. WRONGTYPE, NOPERM) propagate. It ends once the
        event bound with bind_stop_event() is set.

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
            started = self._monotonic()
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
                    self._pace_idle_read(started)
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
            if not results:
                self._pace_idle_read(started)
            yield from self._events_from(results)

    def _stopping(self) -> bool:
        """Whether a bound stop event has been set."""
        return self._stop_event is not None and self._stop_event.is_set()

    def _pace_idle_read(self, started: float) -> None:
        """Wait out what is left of MIN_IDLE_READ_SECONDS since ``started``.

        Called after an empty reply or a read timeout, the two ways a read
        returns without events. Reads that took the block window (the normal
        idle case) don't wait. A bound stop event ends the wait early.
        """
        remaining = MIN_IDLE_READ_SECONDS - (self._monotonic() - started)
        if remaining > 0:
            self._wait(remaining)

    def _wait(self, seconds: float) -> None:
        """Sleep ``seconds``, or until the bound stop event is set."""
        if self._stop_event is not None:
            self._stop_event.wait(seconds)
        else:
            self._sleep(seconds)

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
        self._wait(backoff)
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

        The consume client keeps the URL's timeouts. publish() gets a separate
        client with PUBLISH_CONNECT_TIMEOUT/PUBLISH_SOCKET_TIMEOUT and no
        retries, whatever the URL says. Neither connects before its first command.
        """
        from redis import Redis as RedisClient

        url = _normalize_redis_ssl_params(url)
        return cls(
            client=RedisClient.from_url(url, decode_responses=False),
            prefix=prefix,
            ttl=ttl,
            publish_client_factory=functools.partial(_publish_client_from_url, url),
        )
