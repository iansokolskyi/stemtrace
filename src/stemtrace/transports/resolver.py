"""Host name resolution with a bounded wait, for publishing from task threads.

Socket timeouts don't cover ``socket.getaddrinfo()``, so publishers resolve through
:data:`PUBLISH_RESOLVER`: the caller waits at most its timeout, and answers are
cached and refreshed in the background. A process that forks waits for its lookups
before each fork, then resolves in a short-lived helper process, so a child never
inherits a glibc resolver lock. Under uWSGI (forks without Python's hooks) lookups
run on the calling thread, unbounded. User-facing behavior is in docs/brokers.md.
"""

from __future__ import annotations

import contextlib
import copy
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

AddrInfo = tuple[Any, ...]

logger = logging.getLogger(__name__)

# Seconds an answer is used before it is refreshed (in the background; the
# previous answer keeps being used until a refresh succeeds).
DNS_CACHE_TTL = 30.0

# Seconds a lookup that failed, with no earlier answer to fall back on, is
# remembered: a mistyped host or a dead resolver at boot costs one lookup per
# this interval instead of one per publish.
DNS_NEGATIVE_TTL = 5.0

# Longest a fork() waits for in-process lookups in flight (see prepare_fork()).
FORK_WAIT = 1.0

# In-process lookups younger than this are waited for by every fork (within
# FORK_WAIT each), older ones by one fork only. A lookup can finish, and sort
# a multi-address answer under a glibc lock, at any moment while the resolver
# still answers; glibc gives each try RES_TIMEOUT, 5s by default
# (resolv.conf(5) "timeout"), so an older lookup is waiting on a resolver that
# didn't answer its first try.
RECENT_LOOKUP_AGE = 5.0

# Longest the one-off check that the helper can run this Python may take.
HELPER_CHECK_TIMEOUT = 10.0

# What sys.executable must be called for the helper to be tried at all.
_PYTHON_EXECUTABLE = re.compile(r"^(python|pypy)[\w.-]*(\.exe)?$", re.IGNORECASE)

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
    error: OSError | None = None
    # When the answer (or the last failed refresh) is due for a refresh.
    refresh_at: float = 0.0
    # Set when the lookup in flight ends; None when none is running.
    lookup: threading.Event | None = None


@dataclass
class _InFlight:
    """An in-process lookup that is running."""

    started: float
    # Whether a fork already waited for it.
    waited: bool = False


class _State:
    """Everything a resolver keeps per process, installed as one object.

    Created on first use in each process (not at import, so that under gevent
    the lock is gevent's) and swapped in whole, so threads racing to create
    it all end up using the same lock, cache and flags.
    """

    def __init__(self) -> None:
        """Create an empty cache with its lock."""
        self.lock = threading.Lock()
        # Notified whenever an in-process lookup ends; prepare_fork() waits.
        self.idle = threading.Condition(self.lock)
        self.entries: dict[tuple[Any, ...], _Entry] = {}
        # In-process lookups running, keyed by their done event.
        self.in_flight: dict[threading.Event, _InFlight] = {}
        # Whether prepare_fork() holds the lock (released after the fork).
        self.held_for_fork = False
        # Whether this process has forked: lookups then use the helper.
        self.has_forked = False
        # Whether the helper can run here: None until checked.
        self.helper_usable: bool | None = None
        # When the one-off helper check started (monotonic), None before.
        self.helper_check_started: float | None = None


class BoundedResolver:
    """getaddrinfo() with a bounded wait and a per-process cache.

    Thread- and fork-safe. At most one lookup per host runs at a time; callers
    that arrive meanwhile wait for it (each up to its own timeout). Under uWSGI
    each caller resolves on its own thread, without deduplication.
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
            getaddrinfo: Lookup function (injectable for tests; always run
                in-process). By default ``socket.getaddrinfo`` as it is at
                lookup time (gevent's, if patched), or the helper process
                once this process has forked (see the module docstring).
            monotonic: Clock for the cache (injectable for tests).
            fork_wait: Longest prepare_fork() waits for in-process lookups.
            recent_lookup_age: In-process lookups younger than this are
                waited for by every fork, not just the first.
        """
        self._ttl = ttl
        self._getaddrinfo = getaddrinfo
        self._monotonic = monotonic
        self._fork_wait = fork_wait
        self._recent_lookup_age = recent_lookup_age
        # Per-process state, by PID, created on first use (see _state()), not
        # here: PUBLISH_RESOLVER is built at import, possibly before gevent
        # patches threading, and a real lock contended by greenlets blocks the
        # whole hub.
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

        A cached answer is returned at once (refreshed in the background past
        the TTL). Otherwise the caller waits up to ``timeout``; the lookup keeps
        running and fills the cache. IP addresses are returned without a lookup.

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
        if _under_uwsgi():
            return self._resolve_on_this_thread(state, key)
        with state.lock:
            entry = state.entries.setdefault(key, _Entry())
            now = self._monotonic()
            if entry.addresses is not None:
                if now >= entry.refresh_at:
                    # Best effort: the cached answer serves this caller.
                    with contextlib.suppress(socket.gaierror):
                        self._start_lookup(state, key, entry)
                return entry.addresses
            cached_error = entry.error if entry.lookup is None else None
            if cached_error is None or now >= entry.refresh_at:
                cached_error = None
                done = self._start_lookup(state, key, entry)
        if cached_error is not None:
            # A recent failure (negative caching, see DNS_NEGATIVE_TTL).
            raise _fresh(cached_error)
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
            raise _fresh(error)
        raise socket.gaierror(socket.EAI_AGAIN, f"Could not resolve {host!r}")

    def _resolve_on_this_thread(
        self, state: _State, key: tuple[Any, ...]
    ) -> list[AddrInfo]:
        """Resolve on the calling thread, without a wait bound (under uWSGI).

        uWSGI forks in C without Python's at-fork hooks, so no fork can wait for
        a lookup thread. Answers are cached as in resolve(), but refreshes run
        here too (falling back to the cached answer), without deduplication.
        """
        with state.lock:
            entry = state.entries.setdefault(key, _Entry())
            now = self._monotonic()
            if now < entry.refresh_at:
                if entry.addresses is not None:
                    return entry.addresses
                if entry.error is not None:
                    raise _fresh(entry.error)
        getaddrinfo = self._getaddrinfo or socket.getaddrinfo
        try:
            addresses = _call_getaddrinfo(getaddrinfo, key)
        except OSError as exc:
            with state.lock:
                entry.error = exc
                retry_in = (
                    self._ttl if entry.addresses is not None else DNS_NEGATIVE_TTL
                )
                entry.refresh_at = self._monotonic() + retry_in
                fallback = entry.addresses
            if fallback is not None:
                return fallback
            raise
        with state.lock:
            entry.addresses, entry.error = addresses, None
            entry.refresh_at = self._monotonic() + self._ttl
        return addresses

    def invalidate(
        self, host: str, port: int, family: int = 0, socktype: int = 0, proto: int = 0
    ) -> None:
        """Refresh ``host``'s cached answer now, in the background.

        Called after every cached address failed to connect, so a moved host
        (DNS failover) is picked up before the TTL. The old answer stays in use
        until the refresh succeeds; no-op without an answer. Under uWSGI the
        answer is only marked due for the next resolve().
        """
        state = self._state()
        key = (host, port, family, socktype, proto)
        with state.lock:
            entry = state.entries.get(key)
            if entry is None or entry.addresses is None:
                return
            if _under_uwsgi():
                entry.refresh_at = self._monotonic()
                return
            with contextlib.suppress(socket.gaierror):
                self._start_lookup(state, key, entry)

    def prepare_fork(self) -> None:
        """Before fork(): switch to the helper, wait for lookups, hold the lock.

        Takes the helper spawn lock first, so no helper is starting at fork. Then
        waits up to ``fork_wait`` for in-process lookups that are recent or not
        yet waited for, and keeps the lock so none start until
        parent_after_fork() or child_after_fork(). Later lookups use the helper.
        """
        # No timeout, on purpose: Popen() holds the lock only until the helper
        # has exec'd (normally milliseconds; longer only if exec itself is
        # slow, e.g. on a stalled file system). Forking anyway would give the
        # child the helper's pipes: a pool child never execs, so Popen() would
        # wait for good for its exec status, or the helper's output would
        # never end, leaving that host's lookup stuck in this process. Waiting
        # delays the fork only for as long as Popen() takes.
        _spawn_lock().acquire()
        state = self._state()
        state.lock.acquire()
        state.held_for_fork = True
        # glibc's getaddrinfo() takes locks fork() doesn't reset (seen on 2.41):
        # a child forked mid-lookup can deadlock on its own next lookup.
        state.has_forked = True
        deadline = self._monotonic() + self._fork_wait
        while self._must_wait_for(state):
            remaining = deadline - self._monotonic()
            if remaining <= 0 or not state.idle.wait(remaining):
                for lookup in state.in_flight.values():
                    lookup.waited = True
                return

    def _must_wait_for(self, state: _State) -> bool:
        """Whether an in-process lookup is recent or not yet waited for (lock held)."""
        now = self._monotonic()
        return any(
            not lookup.waited or now - lookup.started < self._recent_lookup_age
            for lookup in state.in_flight.values()
        )

    def parent_after_fork(self) -> None:
        """After fork(), in the parent: let lookups start again; check the helper."""
        state = self._state()
        if state.held_for_fork:
            state.held_for_fork = False
            state.lock.release()
            _spawn_lock().release()
        self._check_helper(state)

    def child_after_fork(self) -> None:
        """After fork(), in the child: start over (it hasn't forked itself).

        The child also gets a fresh spawn lock: prepare_fork() took the
        parent's, and nothing releases it in the child.
        """
        self._states = {os.getpid(): _State()}
        _SPAWN_LOCKS.clear()

    def _check_helper(self, state: _State) -> None:
        """Check once, in the background, that the helper can run here.

        Not under uWSGI: lookups never use the helper there (see
        _under_uwsgi()), and its fork hooks (--py-call-uwsgi-fork-hooks) must
        not leave a ``stemtrace-dns-*`` thread in the process.
        """
        if (
            self._getaddrinfo is not None
            or state.helper_check_started is not None
            or _under_uwsgi()
        ):
            return
        state.helper_check_started = self._monotonic()

        def check() -> None:
            """Record whether the helper can run, unless it's too late."""
            usable = _helper_usable()
            with state.lock:
                if state.helper_usable is None:
                    state.helper_usable = usable
                    if not usable:
                        self._warn_helper_unusable()

        with contextlib.suppress(RuntimeError):  # can't start: stay in-process
            threading.Thread(
                target=check, name="stemtrace-dns-helper-check", daemon=True
            ).start()

    def _warn_helper_unusable(self) -> None:
        """Log that lookups stay in-process.

        Once per process: callers do it right after they set
        ``helper_usable`` from None, under the lock, which happens only once.
        """
        logger.warning(
            "stemtrace can't start %r to resolve broker host names in a helper "
            "process; resolving in-process, and every fork waits up to %gs for "
            "lookups in flight",
            sys.executable,
            self._fork_wait,
        )

    def _helper_check_overdue(self, state: _State) -> None:
        """Give up on a helper check that hasn't answered in time (lock held)."""
        started = state.helper_check_started
        if (
            state.helper_usable is None
            and started is not None
            and self._monotonic() - started >= HELPER_CHECK_TIMEOUT
        ):
            state.helper_usable = False
            self._warn_helper_unusable()

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
        self._helper_check_overdue(state)
        use_helper = state.has_forked and state.helper_usable is True
        use_helper = use_helper and self._getaddrinfo is None
        if not use_helper:
            state.in_flight[done] = _InFlight(started=self._monotonic())
        try:
            threading.Thread(
                target=self._lookup,
                args=(state, key, entry, done, use_helper),
                name=f"stemtrace-dns-{key[0]}",
                daemon=True,
            ).start()
        except RuntimeError as exc:  # e.g. "can't start new thread"
            entry.lookup = None
            state.in_flight.pop(done, None)
            raise socket.gaierror(
                socket.EAI_AGAIN, f"Could not look up {key[0]!r}: {exc}"
            ) from exc
        return done

    def _lookup(
        self,
        state: _State,
        key: tuple[Any, ...],
        entry: _Entry,
        done: threading.Event,
        use_helper: bool,
    ) -> None:
        """Run one lookup and store its outcome (background thread).

        In-process, or in the helper process when ``use_helper`` (this
        process has forked; see the module docstring).
        """
        addresses: list[AddrInfo] | None = None
        error: OSError | None = None
        try:
            getaddrinfo: Callable[..., list[AddrInfo]]
            if use_helper:
                getaddrinfo = _getaddrinfo_in_helper
            else:
                getaddrinfo = self._getaddrinfo or socket.getaddrinfo
            addresses = _call_getaddrinfo(getaddrinfo, key)
        except OSError as exc:  # gaierror and friends: kept for the waiters
            error = exc
        finally:
            # Always release the entry, or every later caller would wait on
            # a lookup that never ends.
            with state.lock:
                if addresses is not None:
                    entry.addresses = addresses
                entry.error = error
                # After a failure too: a stale answer is retried once per TTL,
                # a host with no answer once per DNS_NEGATIVE_TTL.
                retry_in = (
                    self._ttl if entry.addresses is not None else DNS_NEGATIVE_TTL
                )
                entry.refresh_at = self._monotonic() + retry_in
                entry.lookup = None
                state.in_flight.pop(done, None)
                state.idle.notify_all()
            done.set()


def _getaddrinfo_in_helper(
    host: str, port: int, family: int, socktype: int, proto: int
) -> list[AddrInfo]:
    """Run getaddrinfo() in a helper process and return its results.

    The host goes in argv, never through a shell. ``-I -S`` keeps the helper
    isolated from the environment and skips site packages, so it starts fast
    (tens of milliseconds). It is killed and reaped on timeout.

    Raises:
        socket.gaierror: As getaddrinfo() would (same errno), or EAI_AGAIN
            when the helper took longer than HELPER_TIMEOUT, or EAI_FAIL when
            it couldn't run or answer.
    """
    command = [sys.executable, "-I", "-S", "-c", _HELPER_SCRIPT, host]
    command += [str(int(value)) for value in (port or 0, family, socktype, proto)]
    try:
        _, stdout = _run_helper(command, HELPER_TIMEOUT)
        reply = json.loads(stdout)
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


def _helper_usable() -> bool:
    """Whether a helper process running this Python can be started here.

    Not for a frozen application or when sys.executable isn't a Python
    interpreter; otherwise a trivial round trip must succeed.
    """
    executable = sys.executable
    if getattr(sys, "frozen", False) or not hasattr(os, "fork") or not executable:
        return False
    if not _PYTHON_EXECUTABLE.match(Path(executable).name):
        return False
    try:
        returncode, stdout = _run_helper(
            [executable, "-I", "-S", "-c", "print('stemtrace')"], HELPER_CHECK_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return returncode == 0 and stdout.strip() == b"stemtrace"


# One re-entrant lock per process, created on first use (after gevent may have
# patched threading), that helper starts and fork() take (see _run_helper()).
_SPAWN_LOCKS: dict[int, Any] = {}


def _spawn_lock() -> Any:
    """This process's helper spawn lock (an RLock), created on first use.

    Re-entrant because gevent's subprocess starts the helper with os.fork(),
    which runs prepare_fork() on the spawning greenlet while it holds this
    lock. A forked child gets its own (dict.setdefault() keyed by PID).
    """
    pid = os.getpid()
    lock = _SPAWN_LOCKS.get(pid)
    if lock is None:
        lock = _SPAWN_LOCKS.setdefault(pid, threading.RLock())
    return lock


def _run_helper(command: list[str], timeout: float) -> tuple[int, bytes]:
    """Start ``command`` and return its exit code and stdout, within ``timeout``.

    Popen() runs under the spawn lock that prepare_fork() also takes: a child
    forked mid-Popen() would inherit its pipes, and Popen() would wait forever
    for the exec status. The output is read outside the lock.

    Raises:
        subprocess.TimeoutExpired: The helper didn't finish in time; it has
            been killed and reaped.
        OSError: It couldn't be started.
    """
    with _spawn_lock():
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    return process.returncode, stdout


def _call_getaddrinfo(
    getaddrinfo: Callable[..., list[AddrInfo]], key: tuple[Any, ...]
) -> list[AddrInfo]:
    """Run a lookup; any error comes out as an OSError (gaierror).

    Raises:
        OSError: The lookup's own (gaierror and friends), or a gaierror
            (EAI_FAIL) wrapping anything else it raised, e.g. idna's
            UnicodeError, so callers handle one kind of failure.
    """
    try:
        return getaddrinfo(*key)
    except OSError:
        raise
    except Exception as exc:
        raise socket.gaierror(
            socket.EAI_FAIL, f"Could not resolve {key[0]!r}: {exc}"
        ) from exc


def _fresh(error: OSError) -> OSError:
    """A new exception like ``error``, so re-raising never grows one traceback."""
    new = copy.copy(error)
    new.__cause__ = error.__cause__
    return new


def _under_uwsgi() -> bool:
    """Whether this process runs under uWSGI (or pyuwsgi).

    Any uWSGI process may fork in C without Python's at-fork hooks, and
    masterpid(), worker_id() and mule_id() can't tell which (a mule reports
    worker_id() 0, a no-master process masterpid() 0), so the presence of the
    ``uwsgi`` module decides.
    """
    return sys.modules.get("uwsgi") is not None


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


def _register_fork_hooks(
    resolver: BoundedResolver,
    register_at_fork: Callable[..., None] | None,
) -> None:
    """Make fork() run ``resolver``'s hooks (prefork pools fork worker processes).

    Args:
        resolver: The resolver whose process forks.
        register_at_fork: ``os.register_at_fork``; None where it doesn't exist
            (Windows, which has no fork either).
    """
    if register_at_fork is None:
        return
    register_at_fork(
        before=resolver.prepare_fork,
        after_in_parent=resolver.parent_after_fork,
        after_in_child=resolver.child_after_fork,
    )


_register_fork_hooks(PUBLISH_RESOLVER, getattr(os, "register_at_fork", None))
