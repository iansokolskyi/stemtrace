"""Host name resolution with a bounded wait, for publishing from task threads.

Socket timeouts don't cover ``socket.getaddrinfo()``: with an unreachable DNS
resolver a lookup blocks for the resolver's own timeout (glibc: 5s per try and
server), and redis-py and py-amqp both resolve the broker host on every new
connection. Publishers run in the Celery task's thread, so they resolve
through :data:`PUBLISH_RESOLVER` instead: lookups run in the background, the
caller waits at most its timeout, and answers are cached and refreshed in the
background, so a task only ever waits on DNS for a host it has no answer for.

Each lookup runs getaddrinfo() in a short-lived helper process (the same
Python, so the same glibc/NSS, /etc/hosts and resolv.conf), never in a
thread of this process: glibc's getaddrinfo() takes internal locks that
fork() doesn't reset (e.g. while sorting several addresses), so a Celery
prefork child forked while a lookup thread held one deadlocked on its own
next lookup (reproduced with glibc 2.41: 27 of 1000 children hung). Starting
a process execs it at once without calling getaddrinfo() in between, so no
fork of this process can inherit such a lock. Where that isn't possible (no
fork(), e.g. Windows, which also has no fork() hazard; or no Python
executable to run, e.g. an embedded interpreter), lookups run in-process.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
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

# Seconds a helper process may take before it is killed and the lookup fails
# (EAI_AGAIN). Above glibc's worst case with default settings (5s per try,
# 2 tries, up to 3 name servers); callers stop waiting long before, after
# their own timeout, and the answer still fills the cache if it comes.
HELPER_TIMEOUT = 30.0

# Run by the helper process: argv holds host, port, family, socktype, proto;
# prints the getaddrinfo() results (or the gaierror) as JSON.
_HELPER_SCRIPT = """\
import json, socket, sys
host, port, family, socktype, proto = sys.argv[1], *map(int, sys.argv[2:6])
try:
    infos = socket.getaddrinfo(host, port, family, socktype, proto)
except socket.gaierror as exc:
    print(json.dumps({"errno": exc.errno, "message": exc.strerror or str(exc)}))
else:
    print(json.dumps({"infos": [[f, t, p, c, list(a)] for f, t, p, c, a in infos]}))
"""


@dataclass
class _Entry:
    """Cached answer for one lookup key, and the lookup in flight for it."""

    addresses: list[AddrInfo] | None = None
    error: Exception | None = None
    # When the answer (or the last failed refresh) is due for a refresh.
    refresh_at: float = 0.0
    # Set when the lookup in flight ends; None when none is running.
    lookup: threading.Event | None = None


class _State:
    """Everything a resolver keeps per process, installed as one object.

    Created on first use in each process (not at import, so that under gevent
    the lock is gevent's) and swapped in whole, so threads racing to create
    it all end up using the same lock and cache.
    """

    def __init__(self) -> None:
        """Create an empty cache with its lock."""
        self.lock = threading.Lock()
        self.entries: dict[tuple[Any, ...], _Entry] = {}


class BoundedResolver:
    """getaddrinfo() with a bounded wait and a per-process cache.

    Thread-safe. Fork-safe: a forked child starts with an empty cache and a
    fresh lock, since the parent's lookup threads don't exist in it, and no
    lookup runs getaddrinfo() in this process (see the module docstring).
    At most one lookup per host runs at a time; callers that arrive while it
    runs wait for it (each up to its own timeout) instead of starting more.
    """

    def __init__(
        self,
        *,
        ttl: float = DNS_CACHE_TTL,
        getaddrinfo: Callable[..., list[AddrInfo]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize an empty resolver.

        Args:
            ttl: Seconds before a cached answer is refreshed.
            getaddrinfo: Lookup function (injectable for tests). By default
                a helper process runs getaddrinfo() (see the module
                docstring), or ``socket.getaddrinfo`` as it is at lookup time
                (gevent's, if patched) where no helper can run.
            monotonic: Clock for the cache (injectable for tests).
        """
        self._ttl = ttl
        self._getaddrinfo = getaddrinfo
        self._monotonic = monotonic
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
        try:
            threading.Thread(
                target=self._lookup,
                args=(state, key, entry, done),
                name=f"stemtrace-dns-{key[0]}",
                daemon=True,
            ).start()
        except RuntimeError as exc:  # e.g. "can't start new thread"
            entry.lookup = None
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
            getaddrinfo = self._getaddrinfo or _default_getaddrinfo()
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
            done.set()


def _default_getaddrinfo() -> Callable[..., list[AddrInfo]]:
    """The helper process where one can run, else in-process getaddrinfo()."""
    if hasattr(os, "fork") and sys.executable:
        return _getaddrinfo_in_helper
    return socket.getaddrinfo


def _getaddrinfo_in_helper(
    host: str, port: int, family: int, socktype: int, proto: int
) -> list[AddrInfo]:
    """Run getaddrinfo() in a helper process and return its results.

    The host goes in argv, never through a shell. ``-I -S`` keeps the helper
    isolated from the environment and skips site packages, so it starts fast
    (tens of milliseconds). subprocess.run() kills and reaps it on timeout.

    Raises:
        socket.gaierror: As getaddrinfo() would (same errno), or EAI_AGAIN
            when the helper took longer than HELPER_TIMEOUT, or EAI_FAIL when
            it couldn't run or answer.
    """
    command = [sys.executable, "-I", "-S", "-c", _HELPER_SCRIPT, host]
    command += [str(int(value)) for value in (port or 0, family, socktype, proto)]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=HELPER_TIMEOUT,
            check=False,
        )
        reply = json.loads(completed.stdout)
    except subprocess.TimeoutExpired as exc:
        raise socket.gaierror(
            socket.EAI_AGAIN,
            f"Resolving {host!r} timed out after {HELPER_TIMEOUT:g}s",
        ) from exc
    except (OSError, ValueError) as exc:  # couldn't start, or no JSON answer
        raise socket.gaierror(
            socket.EAI_FAIL, f"DNS helper failed for {host!r}: {exc}"
        ) from exc
    if "infos" not in reply:
        raise socket.gaierror(reply.get("errno", socket.EAI_FAIL), reply.get("message"))
    return [
        (
            socket.AddressFamily(f),
            socket.SocketKind(t),
            p,
            c,
            tuple(address),
        )
        for f, t, p, c, address in reply["infos"]
    ]


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
    # Not needed for correctness (state is per PID), but drops the parent's
    # cache in the child at once.
    os.register_at_fork(after_in_child=PUBLISH_RESOLVER.child_after_fork)
