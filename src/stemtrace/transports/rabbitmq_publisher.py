"""One long-lived AMQP connection per process for publishing events to RabbitMQ.

Reusing the connection costs about 0.3 ms per event instead of about 9 ms for a new
one. :meth:`PublisherConnection.publish` is bounded (at most one ``io_timeout`` wait
for the connection, then one bounded connect pass), serializes threads with one
lock, never reuses a parent's socket after fork, and replaces a connection the
broker closed before it loses an event. It raises on failure; the transport turns
that into a breaker failure. User-facing behavior is in ``docs/brokers.md``.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import functools
import logging
import os
import select
import socket
import threading
import time
import weakref
from typing import TYPE_CHECKING, Any, NoReturn

from stemtrace.core.exceptions import is_task_interrupt

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# Longest the exit hook waits for the broker's Close-Ok (and for the lock) so
# interpreter shutdown never hangs on a broker that stopped reading.
CLOSE_TIMEOUT = 0.5

# The most one guarded read of pending frames may take in total (a deadline
# across all its recv() calls, so a frame that trickles in can't extend it),
# and the most frames it reads.
DRAIN_DEADLINE = 0.05
MAX_FRAMES_PER_CHECK = 8
# A publish reads pending frames twice: ours, and py-amqp's own in
# Channel._basic_publish.
DRAINS_PER_PUBLISH = 2

# Waits of one connect pass over one host, in units of connect_timeout: the
# resolver; the TCP connect (one budget shared by all the addresses of the
# name, see rabbitmq._connect_resolved); the TLS handshake; the Start, Tune
# and Open-Ok replies (py-amqp Connection.connect() waits connect_timeout for
# each frame); and, after a failure over TLS, the unwrap py-amqp 5.3
# SSLTransport._shutdown_transport() waits for the peer's close_notify.
CONNECT_WAITS_PER_HOST = 1 + 1 + 1 + 3 + 1
# The steps after connecting (channel open, exchange declare, the write) wait
# io_timeout each.
IO_WAITS_AFTER_CONNECT = 3

# How many times one publish replaces a stuck connection lock before it gives
# up and drops the event (all within the one wait for the lock).
MAX_LOCK_ROUNDS = 3

# How long a waiter lets a holder that took the lock but not yet recorded it
# catch up before it counts as gone.
HOLDER_GRACE = 0.05

# Socket timeout while a connection is torn down without the AMQP handshake:
# over TLS py-amqp 5.3 SSLTransport._shutdown_transport() unwraps the socket,
# which waits for the peer's close_notify.
DISCARD_TIMEOUT = 0.2


def connection_name() -> str:
    """The name RabbitMQ lists the publisher connection of this process under."""
    return f"stemtrace publisher {socket.gethostname()} pid {os.getpid()}"


class PublishError(Exception):
    """An event was not published (the transport counts it as a failure)."""


class PublishDroppedError(PublishError):
    """The event was dropped without an attempt: says nothing about the broker."""


class PublisherBusyError(PublishDroppedError):
    """The connection stayed in use by a holder that is at work for the whole wait."""


class PublishSkippedError(PublishDroppedError):
    """The circuit breaker opened while this event waited for the connection."""


class PublishBlockedError(PublishError):
    """RabbitMQ reports the connection blocked (memory or disk alarm)."""


class _Process:
    """Everything that belongs to one process: the lock and the connection."""

    def __init__(self) -> None:
        """Start with no connection, owned by the current process."""
        self.pid = os.getpid()
        # Created here, not at import: gevent or eventlet may patch
        # ``threading`` after stemtrace is imported, and a real lock would
        # block the whole hub when contended. One lock per process because
        # kombu/py-amqp connections aren't thread-safe; not per thread, since
        # under gevent/eventlet threading.local is per greenlet and Celery
        # starts a greenlet per task (one connection per task).
        self.lock = threading.Lock()
        self.conn: Any = None  # kombu Connection
        self.amqp: Any = None  # its py-amqp connection
        self.sock: Any = None  # the socket of ``amqp``
        self.producer: Any = None
        self.blocked = False
        # Who holds ``lock`` and since when (None while free): lets a waiter
        # tell a stuck holder from a new one, and a thread that leaked the
        # lock from another that waits for it.
        self.holder: int | None = None
        self.held_since: float | None = None
        # Index of the failover host to try first (the one that worked last).
        self.first_host = 0

    def __del__(self) -> None:
        """Drop the connection when its owner goes away, without a ResourceWarning."""
        # Never raises: this may run at interpreter shutdown, with module
        # globals already gone.
        with contextlib.suppress(Exception):
            if self.pid == os.getpid():
                _discard(self)
            else:
                _abandon(self)


# The transports the connect in progress has started (per thread or greenlet).
_connecting: contextvars.ContextVar[list[Any] | None] = contextvars.ContextVar(
    "stemtrace_connecting_transports", default=None
)


def track_connecting_transport(transport: Any) -> None:
    """Remember a py-amqp transport that is connecting, if a connect is in progress.

    A connect abandoned midway (a greenlet killed by a time limit) leaves nothing
    that could close its socket. Track the transport, not its first socket: over
    TLS py-amqp replaces ``transport.sock`` with an SSLSocket.
    """
    transports = _connecting.get()
    if transports is not None:
        transports.append(transport)


def _abandon(proc: _Process) -> None:
    """Drop a connection inherited through fork() without touching the parent's.

    Only this process's copy of the file descriptor is closed: no AMQP close
    (it would send frames the parent's session doesn't expect), no shutdown().
    """
    sock = proc.sock
    proc.conn = proc.amqp = proc.sock = proc.producer = None
    if sock is not None:
        # Not shutdown(): it would end the parent's connection too (py-amqp 5.3
        # _AbstractTransport.close() calls sock.shutdown(SHUT_RDWR)).
        # detach() hands over the descriptor without closing it (-1 if the
        # socket is already closed or another thread detached it), so the
        # close below is a plain close(2).
        with contextlib.suppress(Exception):
            fd = sock.detach()
            if fd >= 0:
                os.close(fd)


def _discard(proc: _Process) -> None:
    """Drop the connection without the AMQP close handshake, never blocking.

    The handshake would wait for the broker's reply, which a stalled
    broker never sends. A short socket timeout bounds the one wait left,
    over TLS (see DISCARD_TIMEOUT).
    """
    amqp, sock = proc.amqp, proc.sock
    proc.conn = proc.amqp = proc.sock = proc.producer = None
    proc.blocked = False
    if sock is not None:
        with contextlib.suppress(Exception):
            sock.settimeout(DISCARD_TIMEOUT)
    if amqp is not None:
        # collect() reads py-amqp's private fields, so it is safe on a
        # connection that is already collected (``amqp.transport`` would
        # reconnect there).
        with contextlib.suppress(Exception):
            amqp.collect()
    if sock is not None:
        # py-amqp 5.0.6 _AbstractTransport.close() calls sock.shutdown()
        # without a try: after an RST it raises ENOTCONN and never reaches
        # sock.close(), and collect() above swallowed that.
        with contextlib.suppress(Exception):
            sock.close()


def _readable(sock: Any) -> bool:
    """Whether a read of ``sock`` would find bytes (or EOF) without waiting."""
    pending = getattr(sock, "pending", None)  # SSLSocket: decrypted, buffered
    if pending is not None and pending():
        return True
    # poll(), not select(): select() fails for descriptors above 1023.
    if hasattr(select, "poll"):
        poller = select.poll()
        poller.register(sock, select.POLLIN)
        return bool(poller.poll(0))
    readable, _, _ = select.select([sock], [], [], 0)  # Windows
    return bool(readable)


def _install_guarded_drain(amqp: Any) -> None:
    """Make ``amqp.drain_events(timeout=0)`` safe against a partly received frame.

    The replacement reads only while the socket is readable, with a positive
    timeout so a partial frame keeps its bytes for the next read, under one
    deadline across all its ``recv()`` calls: it never spins or waits long.
    """
    # py-amqp 5.0.6/5.3.1 Transport._read() retries EAGAIN mid-frame, so a
    # zero-timeout drain of a partial frame (frozen broker) spins under the
    # lock and starves a gevent hub; Channel._basic_publish drains every time.
    base = type(amqp).drain_events

    def drain_events(timeout: float | None = None) -> None:
        """py-amqp ``drain_events``; ``timeout=0`` never spins or waits long."""
        if timeout == 0:
            _drain_pending(amqp)
        base(amqp, timeout)

    amqp.drain_events = drain_events


def _drain_pending(amqp: Any) -> NoReturn:
    """Read the frames that are already there, within DRAIN_DEADLINE.

    Raises:
        TimeoutError: Always, once nothing (more) can be read, as py-amqp
            reports it; anything a frame's handler raises comes first.
    """
    transport = amqp.transport
    # py-amqp 5.0.6/5.3.1 read every frame through transport._quick_recv, and
    # read_frame()/_read() keep a partial frame's bytes when it times out.
    quick_recv = getattr(transport, "_quick_recv", None)
    if quick_recv is not None:
        deadline = time.monotonic() + DRAIN_DEADLINE
        transport._quick_recv = _recv_before(transport, quick_recv, deadline)
    try:
        for _ in range(MAX_FRAMES_PER_CHECK):
            if not _readable(transport.sock):
                break
            amqp.blocking_read(DRAIN_DEADLINE)
    finally:
        if quick_recv is not None:
            transport._quick_recv = quick_recv
    raise TimeoutError


def _recv_before(
    transport: Any, recv: Callable[[int], bytes], deadline: float
) -> Callable[[int], bytes]:
    """A ``recv`` that raises ``socket.timeout`` once ``deadline`` has passed."""

    def bounded(size: int) -> bytes:
        """Receive up to ``size`` bytes, waiting no later than the deadline."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("drain deadline")
        transport.sock.settimeout(remaining)
        return recv(size)

    return bounded


def _set_blocked(
    proc_ref: weakref.ReferenceType[_Process], blocked: bool, *_reason: str
) -> None:
    """Record Connection.Blocked or Unblocked (py-amqp passes a reason text).

    Holds the state weakly: py-amqp keeps this callback, and a strong
    reference would make a cycle that delays closing the connection.
    """
    proc = proc_ref()
    if proc is not None:
        proc.blocked = blocked
    logger.debug(
        "RabbitMQ publisher connection %s", "blocked" if blocked else "unblocked"
    )


_publishers: weakref.WeakSet[PublisherConnection] = weakref.WeakSet()


def _forget_in_child() -> None:
    """After fork() in the child: abandon every inherited publisher connection."""
    for publisher in list(_publishers):
        publisher._forget()


def close_all() -> None:
    """Close every publisher connection of this process (at interpreter exit)."""
    for publisher in list(_publishers):
        publisher.close()


def _register_fork_hook(register_at_fork: Callable[..., None] | None) -> None:
    """Make fork() run :func:`_forget_in_child` in the child.

    Args:
        register_at_fork: ``os.register_at_fork``; None where it doesn't exist
            (Windows, which has no fork either).
    """
    if register_at_fork is not None:
        register_at_fork(after_in_child=_forget_in_child)


atexit.register(close_all)
_register_fork_hook(getattr(os, "register_at_fork", None))


class PublisherConnection:
    """The publisher connection of one transport: one per process, reused."""

    def __init__(
        self,
        *,
        url: str,
        exchange_name: str,
        wrap_connection_class: Callable[[Any], Any],
        connect_timeout: float,
        io_timeout: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize without connecting (the first publish connects).

        Args:
            url: AMQP URL; several hosts separated by ``;`` are failover
                alternates, tried in turn.
            exchange_name: The durable fanout exchange events are published to.
            wrap_connection_class: Adapts kombu's Connection class (e.g. to
                resolve host names boundedly).
            connect_timeout: Seconds each host gets to accept the connection
                (also each frame wait during the AMQP handshake).
            io_timeout: Seconds any later socket read or write may take; also
                how long a publish waits for the connection before it drops
                the event.
            monotonic: Clock for how long the connection has been held
                (injectable for tests).
        """
        self._url = url
        self._exchange_name = exchange_name
        self._wrap_connection_class = wrap_connection_class
        self._connect_timeout = connect_timeout
        self._io_timeout = io_timeout
        self._monotonic = monotonic
        self._hosts = max(len(url.split(";")), 1)
        # How long a task waits for the connection: one IO timeout, however
        # slow the holder is. Longer waits only add latency to tasks.
        self._lock_wait = io_timeout
        self._proc: _Process | None = None
        _publishers.add(self)

    def _attempt_bound(self) -> float:
        """The longest one holder can keep the connection, from the steps it takes.

        The one place this is defined: a lock whose holder is past it is
        stuck, and the breaker's probe timeout (via :attr:`worst_case_s`)
        derives from it.
        """
        connect_pass = CONNECT_WAITS_PER_HOST * self._connect_timeout * self._hosts
        after_connect = IO_WAITS_AFTER_CONNECT * self._io_timeout
        return connect_pass + after_connect + DRAINS_PER_PUBLISH * DRAIN_DEADLINE

    @property
    def worst_case_s(self) -> float:
        """The longest :meth:`publish` can take: the wait for the lock, then an attempt."""
        return self._lock_wait + self._attempt_bound()

    def publish(
        self, payload: dict[str, Any], *, should_attempt: Callable[[], bool]
    ) -> None:
        """Publish ``payload`` on this process's connection, connecting if needed.

        Args:
            payload: JSON-serializable event.
            should_attempt: Whether the circuit breaker still wants this
                attempt; a publish that waited for the connection while the
                breaker opened is skipped instead of connecting to a broker
                that is known to be down.

        Raises:
            PublisherBusyError: The connection stayed in use (by a holder
                that isn't stuck) for the whole wait.
            PublishSkippedError: ``should_attempt()`` was false once the
                connection was free.
            PublishBlockedError: RabbitMQ reports the connection blocked.
            Exception: Whatever kombu or py-amqp raise on a failed connect or
                publish; the connection is discarded and the next publish
                connects again, starting with the next failover host.
        """
        proc = self._acquire(self._process())
        try:
            if not should_attempt():
                raise PublishSkippedError(
                    "publishing paused while this event waited for the connection"
                )
            self._publish_locked(proc, payload)
        finally:
            proc.held_since = None
            proc.holder = None
            proc.lock.release()

    def _acquire(self, proc: _Process) -> _Process:
        """Take the connection lock, starting over if its holder can't release it.

        Waits at most ``_lock_wait`` in total, across all rounds. A lock this
        thread still holds (a signal handler raised inside it), or whose holder
        is gone or past :meth:`_attempt_bound`, is replaced, at most
        MAX_LOCK_ROUNDS times.

        Returns:
            The state whose lock the caller now holds (a new one if the old
            lock was stuck).

        Raises:
            PublisherBusyError: The lock stayed in use by a holder that is at
                work, for the whole wait.
        """
        me = threading.get_ident()
        started = time.monotonic()
        deadline = started + self._lock_wait
        for _ in range(MAX_LOCK_ROUNDS):
            if proc.holder == me and proc.lock.locked():
                proc = self._replace(proc)
                continue
            if proc.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                proc.holder = me
                proc.held_since = self._monotonic()
                return proc
            if not proc.lock.locked():
                continue  # released since the wait ended: try again
            if not self._holder_is_gone(proc, deadline):
                break  # a holder that is at work: the wait is over
            proc = self._replace(proc)
        raise PublisherBusyError(
            f"RabbitMQ publisher connection in use; waited "
            f"{time.monotonic() - started:.2f}s"
        )

    def _holder_is_gone(self, proc: _Process, deadline: float) -> bool:
        """Whether the lock's holder can no longer release it in time.

        True if the lock is taken but nobody recorded taking it (a signal handler
        raised between ``acquire()`` and the ``try``), or the holder is past
        :meth:`_attempt_bound`. The grace for an unrecorded holder comes out of
        ``deadline``.
        """
        since = proc.held_since
        if since is None:
            time.sleep(min(HOLDER_GRACE, max(0.0, deadline - time.monotonic())))
            since = proc.held_since
            if since is None:
                return proc.lock.locked()  # a holder that is releasing isn't gone
        return self._monotonic() - since > self._attempt_bound()

    def _replace(self, stuck: _Process) -> _Process:
        """Start over with a new state; the old one keeps its lock and socket.

        The old connection is left to whoever holds the lock (it is closed
        when the last reference to the state goes), never closed from here.
        """
        if self._proc is stuck:
            logger.warning(
                "RabbitMQ publisher connection was not released by its last user; "
                "starting over with a new connection"
            )
            self._proc = _Process()
        return self._process()

    def close(self) -> None:
        """Close this process's connection if it is idle; never waits long.

        Sends the AMQP close and waits at most CLOSE_TIMEOUT for the broker's
        reply (a broker that stopped reading never sends it), so the broker
        doesn't log an abrupt disconnect and interpreter shutdown doesn't
        hang. A connection in use by another thread is left to the OS.
        """
        proc = self._proc
        if proc is None or proc.pid != os.getpid():
            return
        if not proc.lock.acquire(timeout=CLOSE_TIMEOUT):
            return
        try:
            amqp, sock = proc.amqp, proc.sock
            if amqp is not None:
                with contextlib.suppress(Exception):
                    sock.settimeout(CLOSE_TIMEOUT)
                    amqp.close()
            _discard(proc)
        finally:
            proc.lock.release()

    def _forget(self) -> None:
        """In a forked child: drop the parent's connection (see _abandon())."""
        proc, self._proc = self._proc, None
        if proc is not None:
            _abandon(proc)

    def _process(self) -> _Process:
        """The state of this process, starting over after a fork.

        The at-fork hook already did when Python forked; this catches forks
        that skip it (uWSGI forks in C). Two threads of a hookless child may
        each build a state: they then use separate locks and connections,
        which is safe, and the one built last wins.
        """
        proc = self._proc
        if proc is None or proc.pid != os.getpid():
            if proc is not None:
                _abandon(proc)
            proc = self._proc = _Process()
        return proc

    def _publish_locked(self, proc: _Process, payload: dict[str, Any]) -> None:
        """Publish with the lock held: check the connection, connect, send."""
        if proc.amqp is not None:
            self._check_connection(proc)
        if proc.amqp is None:
            self._connect(proc)
        # Imported with the other kombu names in _connect() (lazily, to keep
        # import-time side effects minimal).
        from kombu import Exchange

        exchange = Exchange(self._exchange_name, type="fanout", durable=True)
        try:
            proc.producer.publish(
                payload,
                exchange=exchange,
                routing_key="",
                # kombu declares once per connection (maybe_declare caches in
                # Connection.declared_entities), so this is no round trip
                # after the first publish.
                declare=[exchange],
                serializer="json",
                # Persistent, so the broker can retain messages in queues.
                # No publisher confirms: waiting for Basic.Ack would hold the
                # lock for a network round trip per event.
                delivery_mode=2,
                retry=False,
            )
        except BaseException:
            # BaseException: a greenlet killed or a signal handler raising
            # mid-write leaves half a frame on the wire, as a timeout does.
            # The fault may be local to this node: start with the next host
            # of a failover URL next time.
            proc.first_host = (proc.first_host + 1) % self._hosts
            _discard(proc)
            raise

    def _check_connection(self, proc: _Process) -> None:
        """Process the frames the broker sent since the last publish.

        Replaces a connection the broker closed (the caller then connects
        anew) and raises while the broker reports it blocked.

        Raises:
            PublishBlockedError: Connection.Blocked arrived and no
                Connection.Unblocked since.
        """
        try:
            # Each frame runs its handler (Blocked and Unblocked call
            # on_blocked and on_unblocked; Close and Channel.Close raise after
            # collecting; EOF raises OSError). drain_events is the guarded
            # one installed by _install_guarded_drain().
            proc.amqp.drain_events(timeout=0)
        except TimeoutError:
            # socket.timeout is TimeoutError since Python 3.10 (the floor). A
            # partly received frame: its bytes are kept for the next read.
            pass
        # Any error here means the connection is unusable: py-amqp raises
        # AMQPError, OSError, even AttributeError for a lost one.
        except Exception as exc:
            if is_task_interrupt(exc):
                # Raised mid-read, so frames may be half consumed: drop the
                # connection, but the interrupt is the task's to see.
                _discard(proc)
                raise
            logger.debug(
                "RabbitMQ publisher connection is gone (%s: %s); reconnecting",
                type(exc).__name__,
                exc,
            )
            _discard(proc)
            return
        if proc.blocked:
            raise PublishBlockedError(
                "RabbitMQ reports the publisher connection blocked (memory or "
                "disk alarm); not publishing until it is unblocked"
            )

    def _connect(self, proc: _Process) -> None:
        """Open the connection, channel and producer (one pass over the hosts)."""
        # kombu is imported lazily to keep import-time side effects minimal.
        from kombu import Connection, Producer

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
        # cost every reconnect a connect timeout.
        first = proc.first_host % self._hosts
        conn = self._connection(self._wrap_connection_class(Connection), first)
        # One connection attempt per host, without sleeping: kombu raises
        # once retries reach max_retries (before switching hosts), so
        # max_retries=0 would never try the alternates of a failover URL.
        # The zero intervals keep kombu from sleeping after a full round.
        opened: list[Any] = []
        token = _connecting.set(opened)
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
            # Every host failed, or one failed in a way kombu doesn't retry
            # on another host (e.g. a refused login): start from the URL's
            # own order next time instead of sticking to this host.
            proc.first_host = 0
            for transport in opened:
                with contextlib.suppress(Exception):
                    if transport.sock is not None:
                        transport.sock.close()
            raise
        finally:
            _connecting.reset(token)
        proc.conn = conn
        proc.first_host = (first + failed_hosts) % self._hosts
        proc.blocked = False
        try:
            # The py-amqp connection (connected, so this doesn't reconnect).
            amqp = proc.amqp = conn.connection
            proc_ref = weakref.ref(proc)
            # py-amqp 5.3 Connection._on_blocked only calls on_blocked: writes
            # keep succeeding until kernel buffers fill, so we track it.
            amqp.on_blocked = functools.partial(_set_blocked, proc_ref, True)
            amqp.on_unblocked = functools.partial(_set_blocked, proc_ref, False)
            _install_guarded_drain(amqp)
            proc.sock = self._bound_io(amqp)
            proc.producer = Producer(conn.channel())
        except BaseException:
            proc.first_host = (first + failed_hosts + 1) % self._hosts
            _discard(proc)
            raise

    def _connection(self, connection_cls: Any, first: int) -> Any:
        """Create a kombu connection for the URL, set to start at host ``first``.

        Switching with kombu's own failover keeps the parameters kombu resolves
        for each entry (a bare ``;rabbit2`` inherits the first entry's
        credentials and vhost); rebuilding the URL with another first entry
        would resolve them differently. No I/O happens here.
        """
        conn = connection_cls(
            self._url,
            connect_timeout=self._connect_timeout,
            # No heartbeats: nothing runs between publishes to send them.
            # Wins over ``?heartbeat=`` in the URL, which a kwarg doesn't
            # (kombu 5.6.1 Transport.establish_connection merges
            # transport_options last).
            transport_options={
                "heartbeat": 0,
                # Shown as the connection's name in RabbitMQ's management UI.
                "client_properties": {"connection_name": connection_name()},
            },
        )
        for _ in range(first):
            conn.maybe_switch_next()
        return conn

    def _bound_io(self, amqp: Any) -> Any:
        """Make every later socket read/write of a py-amqp connection time out.

        Bounds channel open, declare, publish and the close. A no-op for
        transports without a socket attribute.

        Returns:
            The socket (None for such transports).
        """
        transport = getattr(amqp, "transport", None)
        sock = getattr(transport, "sock", None)
        # socket.socket is looked up at call time, so gevent's monkey-patched
        # socket class matches too.
        if isinstance(sock, socket.socket):
            # Not py-amqp's read_timeout option (its reader retries the EAGAIN
            # it causes); frame waits after the handshake use this timeout.
            sock.settimeout(self._io_timeout)
            return sock
        logger.debug(
            "RabbitMQ publish: no socket to bound (got %s); reads and writes "
            "after connecting may block",
            type(sock).__name__,
        )
        return None
