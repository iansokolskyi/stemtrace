"""RabbitMQ transport using kombu.

This adapter publishes stemtrace events to a durable fanout exchange and consumes
from a durable queue bound to that exchange.

Design goals:
- Broker-agnostic event schema (same JSON shape as Redis transport).
- Fire-and-forget publishing: log errors, never raise, and stay cheap while the
  broker is down (Celery tasks must not block). Events that can't be published
  are dropped, never queued.
- Best-effort replay: messages are retained in the queue for up to `ttl` seconds.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import socket
import time
import warnings
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from typing_extensions import Self

from stemtrace.core.events import TaskEvent, WorkerEvent
from stemtrace.transports.breaker import PublishBreaker
from stemtrace.transports.resolver import PUBLISH_RESOLVER

logger = logging.getLogger(__name__)

StreamEvent = TaskEvent | WorkerEvent

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterator

# Reconnect backoff after consume failures: doubles per failure, resets once a
# connection has been established and read from (a drain returned or timed out).
RECONNECT_INITIAL_BACKOFF = 1.0
RECONNECT_MAX_BACKOFF = 30.0

# Seconds a consumer connection attempt may take to open the socket (also the
# per-frame wait during the AMQP handshake). Kept short so a stalled broker
# can't hold up shutdown for long.
CONNECT_TIMEOUT = 2.0

# Longest single wait for broker frames. The consume loop checks the stop event
# between waits, so this bounds how long shutdown waits on an idle connection;
# it must stay well below the server consumer's 5s stop timeout.
DRAIN_TIMEOUT = 1.0

# Publishing runs in the Celery task's thread, so every step is bounded: each
# host of the URL gets one connection attempt with CONNECT_TIMEOUT (no kombu
# retry sleeps), and every later socket read/write (channel open, exchange
# declare, publish, close) times out after PUBLISH_IO_TIMEOUT.
PUBLISH_IO_TIMEOUT = 2.0

# Seconds after which a breaker probe that never reported back (e.g. its
# thread was interrupted by a BaseException) is presumed lost. Far longer than
# any real attempt.
PUBLISH_PROBE_TIMEOUT = 60.0

# PUBLISH_FAILURES_TO_OPEN failed publish attempts in a row (timeouts included)
# open the publish circuit breaker. Until then, a failure loses only its own
# event: a single timeout may come from a healthy broker (e.g. the publishing
# thread waited for the GIL past its socket deadline).
PUBLISH_FAILURES_TO_OPEN = 3

# Deprecated module constants, served with a DeprecationWarning by
# __getattr__ so existing imports keep working until 1.0.
_DEPRECATED_CONSTANTS: dict[str, Any] = {
    # Unused: a single slow failure no longer opens the publish breaker.
    "PUBLISH_SLOW_ATTEMPT": 0.5,
}

# While the breaker is open, events are dropped without connecting for a
# cooldown that doubles per failed retry (RECONNECT_INITIAL_BACKOFF up to
# RECONNECT_MAX_BACKOFF; straight to the maximum after two unconfirmed
# attempts in a row, i.e. while the broker blocks publishers) and resets once
# a publish succeeds. Publish logging is rate-limited per this interval: at
# most one line each for a failure while closed, an outage starting, an outage
# ending, and a report of losses still unreported (see PublishBreaker).
PUBLISH_FAILURE_LOG_INTERVAL = 60.0


class _PublishUnconfirmedError(Exception):
    """The event was sent, but the broker never confirmed the connection close.

    This is how a RabbitMQ that blocks publishers (memory or disk alarm) looks
    from a fresh connection: it stops reading after the publish, so Close-Ok
    never arrives. The event may still be delivered once the alarm clears.
    """


class _StopRequestedError(Exception):
    """Raised between failover hosts to abandon a connect pass on stop."""


@dataclass
class _ReconnectHealth:
    """Reconnect bookkeeping for one consume() iterator."""

    # Consecutive failed connections (each a full pass over the URL's hosts).
    failures: int = 0
    # Delay before the next reconnect attempt. Read when consume() starts,
    # not at import, so a patched constant applies to the first wait too.
    backoff: float = field(default_factory=lambda: RECONNECT_INITIAL_BACKOFF)


def _normalize_prefix(prefix: str) -> str:
    """Normalize an arbitrary prefix into an AMQP-safe name fragment.

    stemtrace uses `prefix` for Redis keys and for URL mount paths. For RabbitMQ,
    we keep it human-readable but avoid characters that commonly cause problems
    in exchange/queue names (notably `/`).
    """
    normalized = prefix.strip()
    normalized = normalized.strip("/")
    normalized = normalized.replace("/", ".")
    return normalized or "stemtrace"


@functools.cache
def _resolving_connection_class(connection_cls: type[Any]) -> type[Any]:
    """Subclass kombu's Connection so py-amqp resolves hosts via PUBLISH_RESOLVER.

    py-amqp 5.3 _AbstractTransport._connect() calls socket.getaddrinfo(),
    which no socket timeout bounds. kombu 5.6 asks get_transport_cls() for
    the transport class each time it creates a transport (including after
    switching failover hosts), so overriding it reaches every host. Other
    transports (e.g. memory://) are returned unchanged.
    """

    class ResolvingConnection(connection_cls):  # type: ignore[misc]  # kombu is untyped
        """kombu Connection whose py-amqp transports resolve hosts boundedly."""

        def get_transport_cls(self) -> Any:
            """The URL's transport, with py-amqp sockets resolved boundedly."""
            return _resolving_transport_class(super().get_transport_cls())

    return ResolvingConnection


@functools.cache
def _resolving_transport_class(transport_cls: Any) -> Any:
    """Subclass a kombu py-amqp transport so its connections resolve boundedly."""
    from kombu.transport import pyamqp

    amqp_connection = getattr(transport_cls, "Connection", None)
    if not (
        isinstance(transport_cls, type) and issubclass(transport_cls, pyamqp.Transport)
    ):
        return transport_cls
    return type(
        f"Resolving{transport_cls.__name__}",
        (transport_cls,),
        {"Connection": _resolving_amqp_connection_class(amqp_connection)},
    )


@functools.cache
def _resolving_amqp_connection_class(connection_cls: type[Any]) -> type[Any]:
    """py-amqp Connection whose transports connect to resolved addresses.

    Mirrors py-amqp 5.3 Connection.Transport() and transport.Transport()
    (SSL when ``ssl`` is set, TCP otherwise), with _connect() taking the
    addresses from PUBLISH_RESOLVER, which waits at most the connect timeout.
    Everything else is py-amqp's own: TLS gets SNI and the hostname check
    from the ``ssl`` options as before, independent of the address used.
    """
    from amqp.transport import SSLTransport, TCPTransport
    from amqp.utils import set_cloexec

    def _connect(self: Any, host: str, port: int, timeout: float | None) -> None:
        """Connect to the first resolved address that accepts (py-amqp's loop)."""
        entries = PUBLISH_RESOLVER.resolve(
            host,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            timeout=timeout or CONNECT_TIMEOUT,
        )
        for i, (family, socktype, proto, _, address) in enumerate(entries):
            try:
                self.sock = socket.socket(family, socktype, proto)
                with contextlib.suppress(NotImplementedError):
                    set_cloexec(self.sock, True)
                self.sock.settimeout(timeout)
                self.sock.connect(address)
            except OSError:
                if self.sock:
                    self.sock.close()
                self.sock = None
                if i + 1 >= len(entries):
                    # The host may have moved (e.g. a DNS failover): refresh
                    # its cached addresses for the next attempt.
                    PUBLISH_RESOLVER.invalidate(
                        host,
                        port,
                        socket.AF_UNSPEC,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                    )
                    raise
            else:
                break

    tcp = type("ResolvingTCPTransport", (TCPTransport,), {"_connect": _connect})
    tls = type("ResolvingSSLTransport", (SSLTransport,), {"_connect": _connect})

    def transport(  # noqa: PLR0917  py-amqp 5.3 Connection.connect() passes these positionally
        _self: Any,
        host: str,
        connect_timeout: float | None,
        ssl: Any = False,
        read_timeout: float | None = None,
        write_timeout: float | None = None,
        socket_settings: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Create the TLS or TCP transport for ``host``."""
        return (tls if ssl else tcp)(
            host,
            connect_timeout=connect_timeout,
            ssl=ssl,
            read_timeout=read_timeout,
            write_timeout=write_timeout,
            socket_settings=socket_settings,
            **kwargs,
        )

    return type(
        f"Resolving{connection_cls.__name__}",
        (connection_cls,),
        {"Transport": transport},
    )


class RabbitMQTransport:
    """RabbitMQ (AMQP) transport using kombu."""

    def __init__(
        self,
        url: str,
        *,
        prefix: str,
        ttl: int,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the RabbitMQ transport.

        Args:
            url: AMQP URL (amqp/amqps/pyamqp). Several hosts separated by
                ``;`` are tried in turn (kombu failover).
            prefix: Namespace for exchange/queue names.
            ttl: Event retention window in seconds (queue message TTL).
            sleep: Sleep function for reconnect backoff when no stop event is
                bound (injectable for tests).
            monotonic: Clock for the publish circuit breaker (injectable for
                tests).
        """
        self._url = url
        self._ttl = ttl
        self._sleep = sleep
        self._monotonic = monotonic
        self._connected: bool | None = None
        self._stop_event: threading.Event | None = None
        self._prefix = _normalize_prefix(prefix)

        # Publish path: the index of the failover host to try first (the last
        # that worked) and the breaker that skips publishing in outages.
        self._publish_first_host = 0
        self._publish_breaker = PublishBreaker(
            "RabbitMQ",
            logger=logger,
            initial_cooldown=RECONNECT_INITIAL_BACKOFF,
            max_cooldown=RECONNECT_MAX_BACKOFF,
            log_interval=PUBLISH_FAILURE_LOG_INTERVAL,
            probe_timeout=PUBLISH_PROBE_TIMEOUT,
            failures_to_open=PUBLISH_FAILURES_TO_OPEN,
            monotonic=monotonic,
        )

        # Fanout exchange to broadcast events.
        self._exchange_name = f"{self._prefix}.events"

        # Per-consumer queue name. We intentionally avoid new env vars/flags and
        # derive a stable-ish id from the hostname.
        self._consumer_id = socket.gethostname()
        self._queue_name = f"{self._exchange_name}.{self._consumer_id}"

        # RabbitMQ expects TTL/expiry in milliseconds.
        ttl_ms = max(ttl, 1) * 1000
        # Make queues self-cleaning if a consumer disappears permanently.
        # Keep it slightly longer than message TTL to allow catching up.
        queue_expires_ms = ttl_ms + 60_000
        self._queue_arguments: dict[str, Any] = {
            "x-message-ttl": ttl_ms,
            "x-expires": queue_expires_ms,
        }

    @property
    def connected(self) -> bool | None:
        """Whether the last drain of the consumer connection reached RabbitMQ.

        None until the first drain of the current consume() completes, True
        after a drain returned or timed out on a live connection, False while
        connecting or draining is failing.
        """
        return self._connected

    def bind_stop_event(self, event: threading.Event) -> None:
        """Stop consume() and interrupt its reconnect backoff when ``event`` is set."""
        self._stop_event = event

    def _stopping(self) -> bool:
        """Whether a bound stop event has been set."""
        return self._stop_event is not None and self._stop_event.is_set()

    @property
    def ttl(self) -> int:
        """TTL in seconds."""
        return self._ttl

    @property
    def exchange_name(self) -> str:
        """The exchange name used for publishing."""
        return self._exchange_name

    @property
    def queue_name(self) -> str:
        """The queue name used for consumption."""
        return self._queue_name

    def publish(self, event: StreamEvent) -> None:
        """Publish an event to RabbitMQ.

        Fire-and-forget: never raises, and events that can't be published are
        dropped, not queued. An attempt tries each host of the URL once, with
        CONNECT_TIMEOUT to connect and PUBLISH_IO_TIMEOUT per socket operation
        after that, and includes the connection close: a broker that stops
        reading after the publish (e.g. a memory or disk alarm blocking
        publishers) makes the Close-Ok wait time out, and the event counts as
        sent but unconfirmed.

        PUBLISH_FAILURES_TO_OPEN failures in a row (timeouts and unconfirmed
        ones included) open a circuit breaker; until then a failure loses
        only its own event. While open,
        events are dropped without connecting for a cooldown (1s, doubling per
        failed retry up to 30s; 30s after two unconfirmed attempts in a row,
        i.e. while the broker blocks publishers; reset by a successful
        publish), so a broker outage costs
        tasks about one bounded attempt per cooldown instead of one per event.
        Only one retry (probe) runs at a time, and failures of attempts that
        were already in flight don't lengthen the cooldown. Per
        PUBLISH_FAILURE_LOG_INTERVAL, at most four WARNING lines are written
        (a failure while closed, an outage starting, an outage ending, and a
        report of losses still unreported), each with the number of events
        lost since the previous line; each event dropped while the breaker
        is open also gets a DEBUG line.

        Args:
            event: Event to publish (TaskEvent or WorkerEvent).
        """
        breaker = self._publish_breaker
        ticket = breaker.allow()
        if ticket is None:
            logger.debug(
                "RabbitMQ publishing paused after failures; dropped event %s",
                self._event_identifier(event),
            )
            return
        try:
            self._publish_payload(event.model_dump(mode="json"))
        except _PublishUnconfirmedError as exc:
            breaker.record_failure(
                ticket,
                self._event_identifier(event),
                exc,
                unconfirmed=True,
            )
        except Exception as exc:  # noqa: BLE001  fire-and-forget: any kombu error feeds the breaker, never the task
            breaker.record_failure(
                ticket,
                self._event_identifier(event),
                exc,
            )
        else:
            breaker.record_success(ticket)

    def _publish_payload(self, payload: dict[str, Any]) -> None:
        """Publish a JSON-serializable payload via kombu.

        Raises whatever kombu/py-amqp raise; publish() handles it.

        Raises:
            _PublishUnconfirmedError: The publish was sent but the connection
                close timed out (the event may still be delivered).

        Worst case per attempt once connected: one PUBLISH_IO_TIMEOUT, since
        a failure drops the socket instead of waiting for Close-Ok too.
        """
        # kombu is imported lazily to keep import-time side effects minimal.
        from kombu import Connection, Exchange, Producer

        exchange = Exchange(self._exchange_name, type="fanout", durable=True)
        # Publish persistent messages so the broker can retain them in queues.
        delivery_mode = 2

        failed_hosts = 0

        def on_host_error(exc: Exception, interval: float) -> None:
            """Count a failed host; kombu switches to the next one after this."""
            nonlocal failed_hosts
            del interval
            failed_hosts += 1
            logger.debug(
                "RabbitMQ publish: failover host unreachable (%s); trying the next",
                exc,
            )

        # Start with the host that worked last, so a dead first host doesn't
        # cost every event a connect timeout.
        hosts = self._publish_host_count()
        first = self._publish_first_host % hosts
        with self._publish_connection(
            _resolving_connection_class(Connection), first
        ) as conn:
            # One connection attempt per host, without sleeping: kombu raises
            # once retries reach max_retries (before switching hosts), so
            # max_retries=0 would never try the alternates of a failover URL.
            # The zero intervals keep kombu from sleeping after a full round.
            try:
                conn.ensure_connection(
                    errback=on_host_error,
                    max_retries=max(len(conn.alt) - 1, 0),
                    interval_start=0,
                    interval_step=0,
                    interval_max=0,
                    reraise_as_library_errors=False,
                )
            except BaseException:
                # Every host failed, or one failed in a way kombu doesn't
                # retry on another host (e.g. a refused login): start from the
                # URL's own order next time instead of sticking to this host.
                self._publish_first_host = 0
                raise
            self._publish_first_host = (first + failed_hosts) % hosts
            # The py-amqp connection (connected, so this doesn't reconnect).
            amqp_conn = conn.connection
            self._bound_publish_io(amqp_conn)
            try:
                channel = conn.channel()
                producer = Producer(channel)
                producer.publish(
                    payload,
                    exchange=exchange,
                    routing_key="",
                    declare=[exchange],
                    serializer="json",
                    delivery_mode=delivery_mode,
                    retry=False,
                )
                # Close here, inside the attempt: kombu's release() (run by
                # the with block) swallows a Close-Ok timeout, so a broker
                # that stopped reading would look healthy. After a successful
                # close, release() has nothing left to do.
                try:
                    amqp_conn.close()
                except TimeoutError as exc:
                    raise _PublishUnconfirmedError(
                        "RabbitMQ did not confirm the connection close after "
                        "the publish (is it blocking publishers?)"
                    ) from exc
            except BaseException:
                # The attempt failed or timed out: drop the socket without the
                # close handshake, which would wait out another timeout.
                with contextlib.suppress(Exception):
                    amqp_conn.collect()
                # The fault may be local to this node: start with the next
                # host of a failover URL next time.
                self._publish_first_host = (first + failed_hosts + 1) % hosts
                raise

    def _publish_host_count(self) -> int:
        """Number of hosts in the URL (``;``-separated failover entries)."""
        return max(len(self._url.split(";")), 1)

    def _publish_connection(self, connection_cls: Any, first: int) -> Any:
        """Create a kombu connection for the URL, set to start at host ``first``.

        It switches hosts the way kombu's own failover does, so every entry
        gets exactly the parameters kombu resolves for it: a bare host
        (``;rabbit2``) takes credentials, port, virtual host and TLS options
        from the first entry; a URL-form entry (``;amqp://rabbit2``) gets no
        credentials (guest/guest) unless it names them, and takes port and
        virtual host from the previous entry only when it omits them.
        Rebuilding the URL with a different first entry would resolve them
        differently (e.g. a bare ``;rabbit2`` would lose the first entry's
        credentials and vhost). No I/O happens here.
        """
        conn = connection_cls(self._url, connect_timeout=CONNECT_TIMEOUT)
        for _ in range(first):
            conn.maybe_switch_next()
        return conn

    @staticmethod
    def _bound_publish_io(amqp_conn: Any) -> None:
        """Make every later socket read/write of a py-amqp connection time out.

        py-amqp's ``read_timeout`` transport option doesn't bound reads (its
        reader retries on the resulting EAGAIN), and its frame waits outside
        the handshake use the socket's own timeout. Setting that timeout on
        the connected socket bounds channel open, declare, publish and close.
        A no-op for transports without a socket attribute.
        """
        transport = getattr(amqp_conn, "transport", None)
        sock = getattr(transport, "sock", None)
        # socket.socket is looked up at call time, so gevent's monkey-patched
        # socket class matches too.
        if isinstance(sock, socket.socket):
            sock.settimeout(PUBLISH_IO_TIMEOUT)
        else:
            logger.debug(
                "RabbitMQ publish: no socket to bound (got %s); reads and writes "
                "after connecting may block",
                type(sock).__name__,
            )

    def _declare_exchange_and_queue(self) -> None:
        """Declare the fanout exchange and this transport's durable consumer queue.

        This is primarily useful for tests and for ensuring the consumer queue exists
        before publishing events (RabbitMQ does not buffer messages in exchanges).
        """
        from kombu import Connection, Exchange, Queue

        exchange = Exchange(self._exchange_name, type="fanout", durable=True)
        queue = Queue(
            name=self._queue_name,
            exchange=exchange,
            routing_key="",
            durable=True,
            queue_arguments=self._queue_arguments,
        )

        with Connection(self._url) as conn:
            channel = conn.channel()
            exchange.maybe_bind(channel)
            queue.maybe_bind(channel)
            exchange.declare()
            queue.declare()

    @staticmethod
    def _event_identifier(event: StreamEvent) -> str:
        """Create a stable identifier for logging."""
        if isinstance(event, TaskEvent):
            return event.task_id
        return f"{event.hostname}:{event.pid}:{event.event_type.value}"

    def consume(self) -> Iterator[StreamEvent]:
        """Blocking iterator that yields events as they arrive.

        Connection and channel errors never end the iterator: the transport
        logs a warning (with a traceback only for the first failure in a row),
        backs off exponentially (capped at RECONNECT_MAX_BACKOFF seconds) and
        reconnects on a new connection, declaring the exchange and queue
        again. Each attempt tries every host of a failover URL
        (``amqp://h1//;amqp://h2//``) once, with CONNECT_TIMEOUT per host, no
        sleep between hosts, and the stop event checked between hosts. Messages kombu can't decode (e.g. not JSON)
        are rejected without requeueing and logged once. The backoff
        resets once a connection has been established and read from (a drain
        returned or timed out); connections that fail before that keep the
        delay growing. The iterator ends once a stop event bound with
        bind_stop_event() is set; that event also interrupts the backoff wait,
        the stop is noticed within DRAIN_TIMEOUT on a live connection, and the
        connection is then dropped without the AMQP close handshake.

        Yields:
            Parsed TaskEvent or WorkerEvent instances.
        """
        # Imported here, outside the retry loop, so a missing kombu fails the
        # first next() instead of being retried as a connection error.
        from kombu import Connection, Exchange, Queue
        from kombu.messaging import Consumer

        exchange = Exchange(self._exchange_name, type="fanout", durable=True)
        queue = Queue(
            name=self._queue_name,
            exchange=exchange,
            routing_key="",
            durable=True,
            queue_arguments=self._queue_arguments,
        )

        # Buffered handoff from kombu callback -> generator.
        pending: deque[StreamEvent] = deque()

        # Unknown until this iterator's first drain completes.
        self._connected = None
        health = _ReconnectHealth()

        while not self._stopping():
            try:
                conn = Connection(self._url, connect_timeout=CONNECT_TIMEOUT)
                try:
                    self._start_consuming(
                        conn, exchange, queue, pending, consumer_cls=Consumer
                    )
                    yield from self._drain_until_stopped(conn, pending, health)
                    return  # It returns only once a stop was requested.
                finally:
                    self._close_consumer_connection(conn)
            except Exception as exc:  # noqa: BLE001  consume loop survives any error: back off and reconnect
                if self._stopping():
                    return
                health.failures += 1
                health.backoff = self._back_off(health.failures, health.backoff, exc)

    def _start_consuming(
        self,
        conn: Any,
        exchange: Any,
        queue: Any,
        pending: deque[StreamEvent],
        *,
        consumer_cls: Any,
    ) -> None:
        """Connect ``conn``, declare the exchange and queue on it, and subscribe.

        Delivered events are appended to ``pending``. Raises whatever kombu
        raises (or _StopRequestedError); consume() backs off and reconnects.
        """
        # One pass over the URL's hosts (";"-separated failover alternates),
        # nothing more: kombu's retry_over_time raises once max_retries is
        # reached, before switching host, and doesn't sleep between hosts
        # within a pass. Its default connect path retries with sleeps that
        # ignore the stop event; retries are consume()'s job. kombu calls
        # `callback` before moving on to the next host, which lets a stop end
        # the pass instead of waiting for every host.
        conn.ensure_connection(
            max_retries=max(len(conn.alt) - 1, 0),
            reraise_as_library_errors=False,
            callback=self._raise_if_stopping,
        )
        channel = conn.channel()
        # Bind fresh copies to this connection's channel. kombu binds an
        # entity only once, so re-binding the shared templates would keep
        # declaring on the first (dead) channel after a reconnect.
        bound_exchange = exchange(channel)
        bound_queue = queue(channel)
        bound_exchange.declare()
        bound_queue.declare()

        consumer = consumer_cls(
            channel,
            queues=[bound_queue],
            callbacks=[functools.partial(self._accept_message, pending)],
            on_decode_error=self._reject_undecodable,
            accept=["json"],
        )
        consumer.consume()

    def _drain_until_stopped(
        self, conn: Any, pending: deque[StreamEvent], health: _ReconnectHealth
    ) -> Iterator[StreamEvent]:
        """Yield events from a subscribed connection until a stop is requested.

        Returns only once the stop event is set (checked after every drain,
        so within DRAIN_TIMEOUT); connection errors propagate to consume().
        """
        while True:
            # Periodic wakeup to allow outer loops to run.
            with contextlib.suppress(TimeoutError, socket.timeout):
                conn.drain_events(timeout=DRAIN_TIMEOUT)
            self._on_drained(health)
            if self._stopping():
                return
            while pending:
                yield pending.popleft()

    def _on_drained(self, health: _ReconnectHealth) -> None:
        """Record a drain that returned or timed out on a live connection.

        The connection is established and was read from, so the outage, if
        any, is over: the backoff resets here and nowhere earlier.
        """
        if health.failures:
            # WARNING, like the failures it ends, so operators who see the
            # outage also see the recovery.
            logger.warning(
                "RabbitMQ consume loop reconnected after %d failure(s)",
                health.failures,
            )
        health.failures = 0
        health.backoff = RECONNECT_INITIAL_BACKOFF
        self._connected = True

    def _accept_message(
        self, pending: deque[StreamEvent], body: Any, message: Any
    ) -> None:
        """Convert broker payload to events, acknowledge/reject messages."""
        try:
            pending.append(self._parse_event(body))
            message.ack()
        except Exception:
            logger.warning(
                "Failed to parse event from RabbitMQ queue %s",
                self._queue_name,
                exc_info=True,
            )
            # Drop malformed messages to avoid poison-pill loops.
            try:
                message.reject(requeue=False)
            except Exception:  # noqa: BLE001  best effort: the message is already logged as malformed
                logger.debug("Failed to reject malformed RabbitMQ message")

    def _reject_undecodable(self, message: Any, exc: Exception) -> None:
        """Drop a message kombu can't decode (e.g. not JSON) instead of failing.

        Without this kombu raises from drain_events, the connection is
        torn down, and the broker redelivers the same message forever.
        """
        logger.warning(
            "Dropping undecodable message from RabbitMQ queue %s: %s",
            self._queue_name,
            exc,
        )
        try:
            message.reject(requeue=False)
        except Exception:
            logger.debug("Failed to reject undecodable RabbitMQ message", exc_info=True)

    def _raise_if_stopping(self) -> None:
        """Abort a multi-host connect pass once a stop has been requested.

        Raises:
            _StopRequestedError: If the bound stop event is set. consume()
                treats any error raised while stopping as a quiet exit.
        """
        if self._stopping():
            raise _StopRequestedError

    def _close_consumer_connection(self, conn: Any) -> None:
        """Close a consumer connection without letting shutdown hang.

        When stopping, the socket is dropped without the AMQP cancel/close
        handshake: those wait for the broker's reply with no timeout, so a
        frozen broker would block shutdown. The broker discards the consumer
        when the socket closes. Otherwise the connection is closed normally.
        Errors are logged at debug level and never raised.
        """
        try:
            if self._stopping() and conn.connected:
                conn.collect()
            else:
                conn.release()
        except Exception:
            logger.debug("Failed to close RabbitMQ consumer connection", exc_info=True)

    def _back_off(self, failures: int, backoff: float, exc: Exception) -> float:
        """Log a consume failure, wait, and return the next backoff delay.

        The full traceback is logged only on the first failure in a row; later
        attempts get a single warning line to avoid flooding the logs.
        """
        self._connected = False
        if failures == 1:
            logger.warning(
                "RabbitMQ consume loop error; retrying in %.1fs",
                backoff,
                exc_info=True,
            )
        else:
            logger.warning(
                "RabbitMQ consume loop still failing (attempt %d): %s; "
                "retrying in %.1fs",
                failures,
                exc,
                backoff,
            )
        if self._stop_event is not None:
            self._stop_event.wait(backoff)
        else:
            self._sleep(backoff)
        return min(backoff * 2, RECONNECT_MAX_BACKOFF)

    @staticmethod
    def _parse_event(payload: Any) -> StreamEvent:
        """Parse payload into appropriate event type."""
        data: Any = payload
        if isinstance(payload, (bytes, bytearray)):
            data = payload.decode()

        if isinstance(data, str):
            parsed: Any = json.loads(data)
        else:
            parsed = data

        if isinstance(parsed, dict):
            if "event_type" in parsed:
                return WorkerEvent.model_validate(parsed)
            if "task_id" in parsed:
                return TaskEvent.model_validate(parsed)
        raise ValueError("Unknown event payload: missing event_type and task_id")

    @classmethod
    def from_url(cls, url: str, *, prefix: str = "stemtrace", ttl: int = 86400) -> Self:
        """Create transport from AMQP URL."""
        return cls(url, prefix=prefix, ttl=ttl)


def __getattr__(name: str) -> Any:
    """Serve deprecated module constants, warning that they go away in 1.0."""
    if name in _DEPRECATED_CONSTANTS:
        return _deprecated_constant(name, stacklevel=3)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _deprecated_constant(name: str, *, stacklevel: int) -> Any:
    """Warn about a deprecated constant and return it.

    ``stacklevel`` counts from this function, so a module ``__getattr__``
    calling it passes 3 to point at the code that used the constant.
    """
    warnings.warn(
        f"{name} is deprecated and unused (a single slow publish failure no "
        "longer opens the circuit breaker); it will be removed in 1.0",
        DeprecationWarning,
        stacklevel=stacklevel,
    )
    return _DEPRECATED_CONSTANTS[name]
