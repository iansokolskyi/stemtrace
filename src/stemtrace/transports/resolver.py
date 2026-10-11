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

# Lookups younger than this are waited for by every fork (within FORK_WAIT
# each). A lookup can finish, and sort a multi-address answer under a glibc
# lock, at any moment while the resolver still answers; glibc gives each try
# RES_TIMEOUT, 5s by default (resolv.conf(5) "timeout"), so a lookup older
# than that is waiting on a resolver that didn't answer its first try.
RECENT_LOOKUP_AGE = 5.0


@dataclass
class _Entry:
    """Cached answer for one lookup key, and the lookup in flight for it."""

    addresses: list[AddrInfo] | None = None
    error: Exception | None = None
    # When the answer (or the last failed refresh) is due for a refresh.
    refresh_at: float = 0.0
    # Set when the lookup in flight ends; None when none is running.
    lookup: threading.Event | None = None


@dataclass
class _InFlight:
    """A lookup thread that is running."""

    started: float
    # Whether a fork already waited for it (so later forks don't).
    waited: bool = False


class _State:
    """Everything a resolver keeps per process, installed as one object.

    Created on first use in each process (not at import, so that under gevent
    the lock is gevent's) and swapped in whole, so threads racing to create
    it all end up using the same lock, condition and maps.
    """

    def __init__(self) -> None:
        """Create an empty cache with its lock."""
        self.lock = threading.Lock()
        # Notified whenever a lookup ends; prepare_fork() waits on it.
        self.idle = threading.Condition(self.lock)
        self.in_flight: dict[threading.Event, _InFlight] = {}
        self.entries: dict[tuple[Any, ...], _Entry] = {}
        # Whether prepare_fork() holds the lock (released after the fork).
        self.held_for_fork = False


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
    PUBLISH_RESOLVER, therefore waits (up to FORK_WAIT per fork) while any
    lookup in flight is younger than RECENT_LOOKUP_AGE or hasn't been waited
    for by a fork yet, including lookups that start or finish meanwhile, and
    keeps new ones from starting until the fork is done. The cost: while a
    lookup is stuck on a dead resolver, forks in its first RECENT_LOOKUP_AGE
    seconds wait about FORK_WAIT each, and one fork after that (measured:
    four forks waited 1.0s, the rest 2-4ms). While it waits on the network a
    lookup holds no such lock (a child forked then resolved normally in the
    same reproduction). Not fully closed: a fork whose FORK_WAIT runs out at
    the very moment a slow lookup completes can still catch it inside glibc
    (measured with a resolver answering after 1.15s and a lookup every ~1.3s:
    1 hung child in 22572 forks, against 5 in 28150 when forks waited only
    once per lookup; in production the resolver refreshes once per TTL or
    after a failed connect). A hung child's lookups time out after the
    caller's bound, so its tasks never block; that child stops publishing.
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
        recent_lookup_age: float = RECENT_LOOKUP_AGE,
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
            recent_lookup_age: Lookups younger than this are waited for by
                every fork, not just the first.
        """
        self._ttl = ttl
        self._getaddrinfo = getaddrinfo
        self._monotonic = monotonic
        self._fork_wait = fork_wait
        self._recent_lookup_age = recent_lookup_age
        # Per-process state, by PID, created on first use (see _state()), not
        # here: PUBLISH_RESOLVER is built at import, possibly before gevent
        # patches threading, and a real lock contended by greenlets blocks the
        # whole hub (reproduced: a deadlock).
        self._states: dict[int, _State] = {}

    def _state(self) -> _State:
        """This process's state, creating it on first use.

        dict.setdefault() installs exactly one _State per PID even when
        threads race here (CPython runs it atomically for int keys); a forked
        child gets its own, without the parent's lock or lookups.
        """
        pid = os.getpid()
        state = self._states.get(pid)
        if state is None:
            state = self._states.setdefault(pid, _State())
        return state

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
        state = self._state()
        key = (host, port, family, socktype, proto)
        with state.lock:
            entry = state.entries.setdefault(key, _Entry())
            if entry.addresses is not None:
                if self._monotonic() >= entry.refresh_at:
                    # Best effort: the cached answer serves this caller.
                    with contextlib.suppress(socket.gaierror):
                        self._start_lookup(state, key, entry)
                return entry.addresses
            done = self._start_lookup(state, key, entry)
        if not done.wait(timeout):
            raise socket.gaierror(
                socket.EAI_AGAIN,
                f"Timed out resolving {host!r} after {timeout:g}s",
            )
        return self._outcome(state, entry, host)

    @staticmethod
    def _outcome(state: _State, entry: _Entry, host: str) -> list[AddrInfo]:
        """The answer a finished lookup left in ``entry``, or its error."""
        with state.lock:
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
        state = self._state()
        key = (host, port, family, socktype, proto)
        with state.lock:
            entry = state.entries.get(key)
            if entry is None or entry.addresses is None:
                return
            with contextlib.suppress(socket.gaierror):
                self._start_lookup(state, key, entry)

    def prepare_fork(self) -> None:
        """Before fork(): wait for lookups that may finish, then hold the lock.

        Waits up to fork_wait in total while any lookup in flight is recent
        (younger than recent_lookup_age) or hasn't been waited for by a fork
        yet; lookups that start or finish while this waits (the lock is
        released meanwhile) are covered too. Whatever is still in flight when
        the time is up is marked as waited for. So a lookup stuck on a dead
        resolver delays the forks of its first recent_lookup_age seconds (by
        up to fork_wait each) and one fork after that. Holding the lock
        afterwards keeps new lookups from starting until parent_after_fork()
        (in the parent) or child_after_fork() (in the child) runs.
        """
        state = self._state()
        state.lock.acquire()
        state.held_for_fork = True
        deadline = self._monotonic() + self._fork_wait
        while self._must_wait_for(state):
            remaining = deadline - self._monotonic()
            if remaining <= 0 or not state.idle.wait(remaining):
                for lookup in state.in_flight.values():
                    lookup.waited = True
                return

    def _must_wait_for(self, state: _State) -> bool:
        """Whether a lookup in flight is recent or not yet waited for (lock held)."""
        now = self._monotonic()
        return any(
            not lookup.waited or now - lookup.started < self._recent_lookup_age
            for lookup in state.in_flight.values()
        )

    def parent_after_fork(self) -> None:
        """After fork(), in the parent: let lookups start again."""
        state = self._state()
        if state.held_for_fork:
            state.held_for_fork = False
            state.lock.release()

    def child_after_fork(self) -> None:
        """After fork(), in the child: start over (the lookup threads are gone)."""
        self._states = {os.getpid(): _State()}

    def _start_lookup(
        self, state: _State, key: tuple[Any, ...], entry: _Entry
    ) -> threading.Event:
        """Start a lookup for ``key`` unless one is running (lock held).

        Raises:
            socket.gaierror: The lookup thread couldn't be started (EAI_AGAIN);
                the entry is left as it was, so a later call tries again.
        """
        if entry.lookup is not None:
            return entry.lookup
        done = threading.Event()
        entry.lookup = done
        state.in_flight[done] = _InFlight(started=self._monotonic())
        try:
            threading.Thread(
                target=self._lookup,
                args=(state, key, entry, done),
                name=f"stemtrace-dns-{key[0]}",
                daemon=True,
            ).start()
        except RuntimeError as exc:  # e.g. "can't start new thread"
            entry.lookup = None
            del state.in_flight[done]
            raise socket.gaierror(
                socket.EAI_AGAIN, f"Could not look up {key[0]!r}: {exc}"
            ) from exc
        return done

    def _lookup(
        self, state: _State, key: tuple[Any, ...], entry: _Entry, done: threading.Event
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
            with state.lock:
                if addresses is not None:
                    entry.addresses = addresses
                entry.error = error
                # After a failure too: a stale answer is retried once per TTL.
                entry.refresh_at = self._monotonic() + self._ttl
                entry.lookup = None
                state.in_flight.pop(done, None)
                state.idle.notify_all()
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
