"""Host name resolution with a bounded wait, for publishing from task threads.

Socket timeouts don't cover ``socket.getaddrinfo()``: with an unreachable DNS
resolver a lookup blocks for the resolver's own timeout (glibc: 5s per try and
server), and redis-py and py-amqp both resolve the broker host on every new
connection. Publishers run in the Celery task's thread, so they resolve
through :data:`PUBLISH_RESOLVER` instead: lookups run in a helper thread, the
caller waits at most its timeout, and answers are cached and refreshed in the
background, so a task only ever waits on DNS for a host it has no answer for.
"""

from __future__ import annotations

import contextlib
import ipaddress
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

AddrInfo = tuple[Any, ...]

# Seconds an answer is used before it is refreshed (in the background; the
# previous answer keeps being used until a refresh succeeds).
DNS_CACHE_TTL = 30.0

# Longest a fork() waits for lookups in flight (see prepare_fork()).
FORK_WAIT = 1.0


@dataclass
class _Entry:
    """Cached answer for one lookup key, and the lookup in flight for it."""

    addresses: list[AddrInfo] | None = None
    error: Exception | None = None
    # When the answer (or the last failed refresh) is due for a refresh.
    refresh_at: float = 0.0
    # Set when the lookup in flight ends; None when none is running.
    lookup: threading.Event | None = None


class BoundedResolver:
    """getaddrinfo() with a bounded wait and a per-process cache.

    Thread-safe. Fork-safe: a forked child starts with an empty cache and a
    fresh lock, since the parent's lookup threads don't exist in it. Lookup
    threads also make fork() itself risky: glibc's getaddrinfo() takes
    internal locks that fork() doesn't reset (e.g. while sorting several
    addresses), and a child forked while a lookup holds one deadlocks on its
    own next lookup (reproduced with glibc 2.41: 27 of 1000 children hung
    while PUBLISH_RESOLVER refreshed a dual-stack name in the background).
    prepare_fork(), registered with os.register_at_fork() for
    PUBLISH_RESOLVER, therefore waits up to FORK_WAIT for lookups in flight
    and keeps new ones from starting until the fork is done. A lookup still
    running after FORK_WAIT is waiting on the network, which holds no such
    lock (a child forked then resolved normally in the same reproduction).
    At most one lookup per host runs at a time; callers that arrive while it
    runs wait for it (each up to its own timeout) instead of starting more.
    """

    def __init__(
        self,
        *,
        ttl: float = DNS_CACHE_TTL,
        getaddrinfo: Callable[..., list[AddrInfo]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        fork_wait: float = FORK_WAIT,
    ) -> None:
        """Initialize an empty resolver.

        Args:
            ttl: Seconds before a cached answer is refreshed.
            getaddrinfo: Lookup function (injectable for tests). By default
                ``socket.getaddrinfo`` as it is at lookup time, so gevent's
                monkey-patched version is used even if stemtrace was imported
                before patching.
            monotonic: Clock for the cache (injectable for tests).
            fork_wait: Longest prepare_fork() waits for lookups in flight.
        """
        self._ttl = ttl
        self._getaddrinfo = getaddrinfo
        self._monotonic = monotonic
        self._fork_wait = fork_wait
        self._start_over()

    def _start_over(self) -> None:
        """Reset the cache and lock (at init and in a forked child)."""
        self._lock = threading.Lock()
        # Notified whenever a lookup ends; prepare_fork() waits on it.
        self._idle = threading.Condition(self._lock)
        self._lookups = 0
        self._pid = os.getpid()
        self._entries: dict[tuple[Any, ...], _Entry] = {}

    def resolve(
        self,
        host: str,
        port: int,
        family: int = 0,
        socktype: int = 0,
        proto: int = 0,
        *,
        timeout: float,
    ) -> list[AddrInfo]:
        """Return getaddrinfo() results for ``host``, waiting at most ``timeout``.

        A cached answer is returned at once (and refreshed in the background
        once it is older than the TTL). Without one, the caller waits for the
        lookup up to ``timeout`` seconds; the lookup keeps running after that
        and fills the cache for later callers. IP addresses are returned
        without a lookup.

        Raises:
            socket.gaierror: The lookup failed, or didn't finish within
                ``timeout`` (EAI_AGAIN).
        """
        if _is_ip_address(host):
            return socket.getaddrinfo(
                host, port, family, socktype, proto, socket.AI_NUMERICHOST
            )
        _check_host_name(host)
        if os.getpid() != self._pid:
            self._start_over()
        key = (host, port, family, socktype, proto)
        with self._lock:
            entry = self._entries.setdefault(key, _Entry())
            if entry.addresses is not None:
                if self._monotonic() >= entry.refresh_at:
                    # Best effort: the cached answer serves this caller.
                    with contextlib.suppress(socket.gaierror):
                        self._start_lookup(key, entry)
                return entry.addresses
            done = self._start_lookup(key, entry)
        if not done.wait(timeout):
            raise socket.gaierror(
                socket.EAI_AGAIN,
                f"Timed out resolving {host!r} after {timeout:g}s",
            )
        return self._outcome(entry, host)

    def _outcome(self, entry: _Entry, host: str) -> list[AddrInfo]:
        """The answer a finished lookup left in ``entry``, or its error."""
        with self._lock:
            addresses, error = entry.addresses, entry.error
        if addresses is not None:
            return addresses
        if error is not None:
            raise error
        raise socket.gaierror(socket.EAI_AGAIN, f"Could not resolve {host!r}")

    def invalidate(
        self, host: str, port: int, family: int = 0, socktype: int = 0, proto: int = 0
    ) -> None:
        """Refresh ``host``'s cached answer now, in the background.

        Called after connecting to every cached address failed, so the next
        attempt uses the new address if the host moved (e.g. a DNS failover)
        instead of waiting for the TTL. The cached answer stays in use until
        the refresh succeeds. Does nothing for a host without an answer.
        """
        if os.getpid() != self._pid:
            self._start_over()
        with self._lock:
            entry = self._entries.get((host, port, family, socktype, proto))
            if entry is None or entry.addresses is None:
                return
            with contextlib.suppress(socket.gaierror):
                self._start_lookup((host, port, family, socktype, proto), entry)

    def prepare_fork(self) -> None:
        """Before fork(): wait up to fork_wait for lookups, then hold the lock.

        Holding the lock keeps new lookups from starting until
        parent_after_fork() (in the parent) or child_after_fork() (in the
        child) runs.
        """
        self._lock.acquire()
        self._idle.wait_for(lambda: self._lookups == 0, timeout=self._fork_wait)

    def parent_after_fork(self) -> None:
        """After fork(), in the parent: let lookups start again."""
        self._lock.release()

    def child_after_fork(self) -> None:
        """After fork(), in the child: start over (the lookup threads are gone)."""
        self._start_over()

    def _start_lookup(self, key: tuple[Any, ...], entry: _Entry) -> threading.Event:
        """Start a lookup for ``key`` unless one is running (lock held).

        Raises:
            socket.gaierror: The lookup thread couldn't be started (EAI_AGAIN);
                the entry is left as it was, so a later call tries again.
        """
        if entry.lookup is not None:
            return entry.lookup
        done = threading.Event()
        entry.lookup = done
        self._lookups += 1
        try:
            threading.Thread(
                target=self._lookup,
                args=(key, entry, done),
                name=f"stemtrace-dns-{key[0]}",
                daemon=True,
            ).start()
        except RuntimeError as exc:  # e.g. "can't start new thread"
            entry.lookup = None
            self._lookups -= 1
            raise socket.gaierror(
                socket.EAI_AGAIN, f"Could not look up {key[0]!r}: {exc}"
            ) from exc
        return done

    def _lookup(
        self, key: tuple[Any, ...], entry: _Entry, done: threading.Event
    ) -> None:
        """Run one getaddrinfo() call and store its outcome (helper thread)."""
        host, port, family, type_, proto = key
        addresses: list[AddrInfo] | None = None
        error: OSError | None = None
        try:
            getaddrinfo = self._getaddrinfo or socket.getaddrinfo
            addresses = getaddrinfo(host, port, family, type_, proto)
        except OSError as exc:  # gaierror and friends: kept for the waiters
            error = exc
        except Exception as exc:  # noqa: BLE001  e.g. idna's UnicodeError: reported to the waiters, never kills the entry
            error = socket.gaierror(
                socket.EAI_FAIL, f"Could not resolve {host!r}: {exc}"
            )
            error.__cause__ = exc
        finally:
            # Always release the entry, or every later caller would wait on
            # a lookup that never ends.
            with self._lock:
                if addresses is not None:
                    entry.addresses = addresses
                entry.error = error
                # After a failure too: a stale answer is retried once per TTL.
                entry.refresh_at = self._monotonic() + self._ttl
                entry.lookup = None
                self._lookups -= 1
                self._idle.notify_all()
            done.set()


def _check_host_name(host: str) -> None:
    """Fail fast on a host name no DNS server would accept.

    Raises:
        socket.gaierror: The name can't be IDNA-encoded (e.g. a label longer
            than 63 characters), as getaddrinfo() would report on lookup.
    """
    try:
        host.encode("idna")
    except UnicodeError as exc:
        raise socket.gaierror(
            socket.EAI_NONAME, f"Invalid host name {host!r}: {exc}"
        ) from exc


def _is_ip_address(host: str) -> bool:
    """Whether ``host`` is an IPv4/IPv6 address rather than a name."""
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


# Shared by the publishers of every transport in this process.
PUBLISH_RESOLVER = BoundedResolver()

if hasattr(os, "register_at_fork"):  # POSIX; prefork pools fork worker processes
    os.register_at_fork(
        before=PUBLISH_RESOLVER.prepare_fork,
        after_in_parent=PUBLISH_RESOLVER.parent_after_fork,
        after_in_child=PUBLISH_RESOLVER.child_after_fork,
    )
