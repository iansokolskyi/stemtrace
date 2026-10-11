"""Tests for the bounded, cached host name resolver used by publishers."""

import contextlib
import os
import socket
import sys
import threading
import time
import warnings
from typing import Any

import pytest

from stemtrace.transports.resolver import PUBLISH_RESOLVER, BoundedResolver

AddrInfo = tuple[Any, ...]
ADDRESS: list[AddrInfo] = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 6379))
]
NEW_ADDRESS: list[AddrInfo] = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 6379))
]
# Generous: only reached when a test is broken, never on the happy path.
JOIN_S = 5.0


class FakeClock:
    """Monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class FakeDns:
    """getaddrinfo stand-in: answers from ``answers`` or blocks until released."""

    def __init__(self) -> None:
        self.answers: list[list[AddrInfo] | Exception] = [ADDRESS]
        self.calls: list[str] = []
        self.release = threading.Event()
        self.release.set()
        self.done = threading.Event()

    def __call__(self, host: str, port: int, *args: Any) -> list[AddrInfo]:
        self.calls.append(host)
        try:
            self.release.wait(JOIN_S)
            answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            if isinstance(answer, Exception):
                raise answer
            return answer
        finally:
            self.done.set()


def _make(dns: FakeDns, clock: FakeClock, *, ttl: float = 30.0) -> BoundedResolver:
    return BoundedResolver(ttl=ttl, getaddrinfo=dns, monotonic=clock)


def _wait_lookup(dns: FakeDns) -> None:
    """Wait until the resolver's lookup threads have stored their outcome."""
    assert dns.done.wait(JOIN_S)
    dns.done.clear()
    for thread in threading.enumerate():
        if thread.name.startswith("stemtrace-dns-"):
            thread.join(JOIN_S)


class TestBoundedResolver:
    def test_resolve_returns_lookup_result(self) -> None:
        dns, clock = FakeDns(), FakeClock()

        assert _make(dns, clock).resolve("redis.local", 6379, timeout=1.0) == ADDRESS
        assert dns.calls == ["redis.local"]

    def test_resolve_within_ttl_uses_cache(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        resolver = _make(dns, clock)
        resolver.resolve("redis.local", 6379, timeout=1.0)
        clock.now += 29.9

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS
        assert dns.calls == ["redis.local"]

    def test_resolve_ip_literal_skips_dns(self) -> None:
        dns, clock = FakeDns(), FakeClock()

        result = _make(dns, clock).resolve("127.0.0.1", 6379, timeout=1.0)

        assert dns.calls == []
        assert result[0][4][:2] == ("127.0.0.1", 6379)

    def test_resolve_with_hung_dns_gives_up_after_timeout(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        dns.release.clear()  # the resolver never answers
        resolver = _make(dns, clock)

        with pytest.raises(socket.gaierror, match=r"redis\.local.*0\.05s"):
            resolver.resolve("redis.local", 6379, timeout=0.05)
        dns.release.set()

    def test_concurrent_misses_share_one_lookup(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        dns.release.clear()
        resolver = _make(dns, clock)
        errors: list[BaseException] = []

        def attempt() -> None:
            try:
                resolver.resolve("redis.local", 6379, timeout=0.05)
            except socket.gaierror as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(JOIN_S)
        dns.release.set()
        _wait_lookup(dns)

        assert len(errors) == 8
        assert dns.calls == ["redis.local"]
        # The lookup that outlived the callers still fills the cache.
        assert resolver.resolve("redis.local", 6379, timeout=0.05) == ADDRESS
        assert dns.calls == ["redis.local"]

    def test_expired_entry_is_served_while_refreshing_in_background(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        dns.answers = [ADDRESS, NEW_ADDRESS]
        resolver = _make(dns, clock)
        resolver.resolve("redis.local", 6379, timeout=1.0)
        _wait_lookup(dns)
        dns.release.clear()  # the refresh hangs
        clock.now += 31.0

        # The caller never waits on the refresh.
        assert resolver.resolve("redis.local", 6379, timeout=0.0) == ADDRESS
        dns.release.set()
        _wait_lookup(dns)

        assert resolver.resolve("redis.local", 6379, timeout=0.0) == NEW_ADDRESS
        assert dns.calls == ["redis.local", "redis.local"]

    def test_failed_refresh_keeps_last_answer_and_retries_after_ttl(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        dns.answers = [ADDRESS, socket.gaierror(socket.EAI_AGAIN, "no dns")]
        resolver = _make(dns, clock)
        resolver.resolve("redis.local", 6379, timeout=1.0)
        _wait_lookup(dns)
        clock.now += 31.0

        resolver.resolve("redis.local", 6379, timeout=1.0)
        _wait_lookup(dns)
        for _ in range(5):
            assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS

        assert len(dns.calls) == 2
        clock.now += 30.0
        resolver.resolve("redis.local", 6379, timeout=1.0)
        _wait_lookup(dns)
        assert len(dns.calls) == 3

    def test_failed_first_lookup_raises_its_error(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        dns.answers = [socket.gaierror(socket.EAI_NONAME, "unknown host")]

        with pytest.raises(socket.gaierror, match="unknown host"):
            _make(dns, clock).resolve("nope.local", 6379, timeout=1.0)

    def test_failed_first_lookup_is_retried_on_next_attempt(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        dns.answers = [socket.gaierror(socket.EAI_AGAIN, "no dns"), ADDRESS]
        resolver = _make(dns, clock)
        with pytest.raises(socket.gaierror):
            resolver.resolve("redis.local", 6379, timeout=1.0)

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS

    def test_forked_child_starts_with_empty_cache_and_fresh_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dns, clock = FakeDns(), FakeClock()
        resolver = _make(dns, clock)
        resolver.resolve("redis.local", 6379, timeout=1.0)
        _wait_lookup(dns)
        resolver._state().lock.acquire()  # held by a parent thread at fork time
        parent_pid = os.getpid()

        monkeypatch.setattr(os, "getpid", lambda: parent_pid + 1)

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS
        assert dns.calls == ["redis.local", "redis.local"]


class TestBoundedResolverRobustness:
    """A failed lookup never leaves a host stuck; bad names fail fast."""

    def test_thread_start_failure_raises_and_next_attempt_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dns, clock = FakeDns(), FakeClock()
        resolver = _make(dns, clock)

        def cannot_start(self: threading.Thread) -> None:
            raise RuntimeError("can't start new thread")

        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, "start", cannot_start)
            with pytest.raises(socket.gaierror, match="can't start new thread"):
                resolver.resolve("redis.local", 6379, timeout=1.0)

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS

    def test_unexpected_lookup_error_is_raised_and_next_attempt_retries(
        self,
    ) -> None:
        dns, clock = FakeDns(), FakeClock()
        dns.answers = [UnicodeError("label too long"), ADDRESS]
        resolver = _make(dns, clock)

        with pytest.raises(socket.gaierror, match="label too long"):
            resolver.resolve("redis.local", 6379, timeout=1.0)

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS
        assert dns.calls == ["redis.local", "redis.local"]

    def test_invalid_host_name_fails_fast_without_lookup(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        host = "a" * 64 + ".example.com"  # DNS labels are at most 63 octets

        with pytest.raises(socket.gaierror, match="Invalid host name"):
            _make(dns, clock).resolve(host, 6379, timeout=1.0)

        assert dns.calls == []

    def test_resolve_uses_socket_getaddrinfo_patched_after_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gevent patches socket.getaddrinfo after stemtrace may be imported."""
        resolver = BoundedResolver(monotonic=FakeClock())
        dns = FakeDns()
        monkeypatch.setattr(socket, "getaddrinfo", dns)

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS
        assert dns.calls == ["redis.local"]


class TestBoundedResolverInvalidate:
    """A failed connect refreshes the host's answer at once."""

    def test_invalidate_refreshes_cached_answer_before_ttl(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        dns.answers = [ADDRESS, NEW_ADDRESS]
        resolver = _make(dns, clock)
        resolver.resolve("redis.local", 6379, timeout=1.0)
        _wait_lookup(dns)

        resolver.invalidate("redis.local", 6379)
        _wait_lookup(dns)

        assert resolver.resolve("redis.local", 6379, timeout=0.0) == NEW_ADDRESS

    def test_invalidate_unknown_host_does_nothing(self) -> None:
        dns, clock = FakeDns(), FakeClock()

        _make(dns, clock).invalidate("redis.local", 6379)

        assert dns.calls == []


class TestBoundedResolverFork:
    """Forking waits (boundedly) for lookups in flight, then blocks new ones.

    glibc's getaddrinfo() takes internal locks that fork() doesn't reset; a
    child forked while a lookup thread holds one deadlocks on its own first
    lookup of the same kind.
    """

    def test_prepare_fork_waits_for_lookup_in_flight(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        dns.release.clear()
        resolver = BoundedResolver(getaddrinfo=dns, monotonic=clock, fork_wait=JOIN_S)
        with pytest.raises(socket.gaierror):
            resolver.resolve("redis.local", 6379, timeout=0.0)
        threading.Timer(0.05, dns.release.set).start()

        resolver.prepare_fork()
        try:
            in_flight = len(resolver._state().in_flight)
        finally:
            resolver.parent_after_fork()

        assert in_flight == 0
        assert resolver.resolve("redis.local", 6379, timeout=0.0) == ADDRESS

    def test_prepare_fork_gives_up_after_bound(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        dns.release.clear()  # the lookup never ends on its own
        resolver = BoundedResolver(getaddrinfo=dns, monotonic=clock, fork_wait=0.05)
        with pytest.raises(socket.gaierror):
            resolver.resolve("redis.local", 6379, timeout=0.0)

        resolver.prepare_fork()
        try:
            in_flight = len(resolver._state().in_flight)
        finally:
            resolver.parent_after_fork()
            dns.release.set()

        assert in_flight == 1

    def test_prepare_fork_holds_lock_until_after_fork(self) -> None:
        resolver = BoundedResolver(getaddrinfo=FakeDns(), monotonic=FakeClock())

        resolver.prepare_fork()
        try:
            assert resolver._state().lock.acquire(blocking=False) is False
        finally:
            resolver.parent_after_fork()

        assert resolver._state().lock.acquire(blocking=False) is True
        resolver._state().lock.release()

    def test_child_after_fork_starts_over_with_free_lock(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        resolver = _make(dns, clock)
        resolver.resolve("redis.local", 6379, timeout=1.0)
        resolver.prepare_fork()

        resolver.child_after_fork()

        assert resolver._state().lock.acquire(blocking=False) is True
        resolver._state().lock.release()
        assert resolver._state().entries == {}

    def test_publish_resolver_is_registered_for_fork(self) -> None:
        """A real fork while another thread holds the resolver's lock."""
        PUBLISH_RESOLVER.invalidate("redis.local", 6379)  # creates the lock
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with PUBLISH_RESOLVER._state().lock:
                held.set()
                release.wait(JOIN_S)

        holder = threading.Thread(target=hold)
        holder.start()
        assert held.wait(JOIN_S)
        threading.Timer(0.2, release.set).start()
        with warnings.catch_warnings():
            # Python warns that forking a multi-threaded process may deadlock:
            # that is what the registered fork hooks guard against.
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child process
            ok = PUBLISH_RESOLVER._state().lock.acquire(timeout=1.0)
            os._exit(0 if ok else 1)
        _, status = os.waitpid(pid, 0)
        holder.join(JOIN_S)

        assert os.waitstatus_to_exitcode(status) == 0
        assert PUBLISH_RESOLVER._state().lock.acquire(blocking=False) is True
        PUBLISH_RESOLVER._state().lock.release()


class TestBoundedResolverForkWaitsOncePerLookup:
    """Each lookup is waited for by at most one fork (up to FORK_WAIT)."""

    def test_prepare_fork_waits_for_old_lookup_no_fork_waited_for(self) -> None:
        """An older lookup may be finishing inside glibc's locked section."""
        dns, clock = FakeDns(), FakeClock()
        dns.release.clear()
        resolver = BoundedResolver(getaddrinfo=dns, monotonic=clock, fork_wait=JOIN_S)
        with pytest.raises(socket.gaierror):
            resolver.resolve("redis.local", 6379, timeout=0.0)
        clock.now += JOIN_S + 0.1  # in flight longer than fork_wait already
        threading.Timer(0.05, dns.release.set).start()

        resolver.prepare_fork()
        try:
            in_flight = len(resolver._state().in_flight)
        finally:
            resolver.parent_after_fork()

        assert in_flight == 0  # it waited for the lookup to finish

    def _forks(self, resolver: BoundedResolver, n: int) -> list[float]:
        """Run ``n`` fork preparations; return how long each one waited."""
        durations = []
        for _ in range(n):
            started = time.monotonic()
            resolver.prepare_fork()
            resolver.parent_after_fork()
            durations.append(time.monotonic() - started)
        return durations

    def test_recent_lookup_is_waited_for_by_every_fork(self) -> None:
        """A lookup younger than RECENT_LOOKUP_AGE may still finish (and sort
        its answer under glibc's lock) at any moment, so no fork skips it."""
        dns, clock = FakeDns(), FakeClock()
        dns.release.clear()
        resolver = BoundedResolver(
            getaddrinfo=dns, monotonic=clock, fork_wait=0.1, recent_lookup_age=5.0
        )
        with pytest.raises(socket.gaierror):
            resolver.resolve("redis.local", 6379, timeout=0.0)
        clock.now += 4.9  # still recent

        durations = self._forks(resolver, 3)
        dns.release.set()

        assert min(durations) >= 0.1

    def test_stuck_lookup_past_recent_age_delays_only_one_fork(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        dns.release.clear()  # stuck, like a lookup against a dead resolver
        resolver = BoundedResolver(
            getaddrinfo=dns, monotonic=clock, fork_wait=0.2, recent_lookup_age=5.0
        )
        with pytest.raises(socket.gaierror):
            resolver.resolve("redis.local", 6379, timeout=0.0)
        clock.now += 5.0  # no longer recent, and no fork waited for it yet

        durations = self._forks(resolver, 3)
        dns.release.set()

        assert durations[0] >= 0.2
        assert max(durations[1:]) < 0.1

    def test_prepare_fork_waits_for_lookup_started_while_waiting(self) -> None:
        first, second = FakeDns(), FakeDns()
        first.release.clear()
        second.release.clear()

        def dns(host: str, *args: Any) -> list[AddrInfo]:
            return (first if host == "a.local" else second)(host, *args)

        resolver = BoundedResolver(
            getaddrinfo=dns, monotonic=FakeClock(), fork_wait=JOIN_S
        )
        with pytest.raises(socket.gaierror):
            resolver.resolve("a.local", 6379, timeout=0.0)

        def start_second_then_release_first() -> None:
            with contextlib.suppress(socket.gaierror):
                resolver.resolve("b.local", 6379, timeout=0.0)
            first.release.set()
            threading.Timer(0.1, second.release.set).start()

        threading.Timer(0.05, start_second_then_release_first).start()

        resolver.prepare_fork()
        try:
            in_flight = len(resolver._state().in_flight)
        finally:
            resolver.parent_after_fork()

        assert in_flight == 0  # it waited for the second lookup too


class TestBoundedResolverCreatesLocksLazily:
    """gevent may patch threading after stemtrace is imported (and
    PUBLISH_RESOLVER built): the lock must be created when first used, so
    it is gevent's cooperative lock rather than a real one that blocks the
    hub when contended."""

    def test_lock_is_created_on_first_use(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resolver = BoundedResolver(getaddrinfo=FakeDns(), monotonic=FakeClock())
        created: list[object] = []
        real_lock = threading.Lock

        def recording_lock() -> Any:
            lock = real_lock()
            created.append(lock)
            return lock

        monkeypatch.setattr(threading, "Lock", recording_lock)

        resolver.resolve("redis.local", 6379, timeout=1.0)

        assert resolver._state().lock in created


class TestBoundedResolverConcurrentFirstUse:
    """Threads resolving at once in a fresh process share one state."""

    def test_concurrent_first_resolves_share_one_state(self) -> None:
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)  # switch threads as often as possible
        failures: list[BaseException] = []
        try:
            for _ in range(200):
                resolver = BoundedResolver(getaddrinfo=FakeDns(), monotonic=FakeClock())
                start = threading.Barrier(8)

                def resolve(
                    resolver: BoundedResolver = resolver,
                    start: threading.Barrier = start,
                ) -> None:
                    start.wait()
                    try:
                        assert (
                            resolver.resolve("redis.local", 6379, timeout=0.5)
                            == ADDRESS
                        )
                    except BaseException as exc:  # noqa: BLE001  collected for the assertion
                        failures.append(exc)

                threads = [threading.Thread(target=resolve) for _ in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(JOIN_S)
                if failures:
                    break
        finally:
            sys.setswitchinterval(previous)

        assert failures == []
