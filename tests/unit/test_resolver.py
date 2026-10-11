"""Tests for the bounded, cached host name resolver used by publishers."""

import os
import socket
import sys
import threading
import warnings
from typing import Any

import pytest

from stemtrace.transports import resolver as resolver_module
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

    def test_default_lookup_runs_in_helper_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """glibc's getaddrinfo() never runs in this process's threads, so a
        fork() can't inherit a lock it holds (see resolver docstring)."""
        resolver = BoundedResolver(monotonic=FakeClock())
        helper = FakeDns()
        monkeypatch.setattr(resolver_module, "_getaddrinfo_in_helper", helper)

        def in_process(*args: Any) -> Any:
            raise AssertionError("getaddrinfo() ran in this process")

        monkeypatch.setattr(socket, "getaddrinfo", in_process)

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS
        assert helper.calls == ["redis.local"]

    @pytest.mark.parametrize("no_helper", ["no_fork", "no_executable"])
    def test_default_lookup_without_helper_uses_socket_getaddrinfo(
        self, no_helper: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No fork() (Windows) or no Python executable (embedded interpreters):
        resolve in-process, with socket.getaddrinfo looked up at call time so
        gevent's patched version is used."""
        if no_helper == "no_fork":
            monkeypatch.delattr(os, "fork", raising=False)
        else:
            monkeypatch.setattr(sys, "executable", "")
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
    """A forked child starts with its own state, whatever the parent held."""

    def test_child_after_fork_starts_over_with_free_lock(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        resolver = _make(dns, clock)
        resolver.resolve("redis.local", 6379, timeout=1.0)
        resolver._state().lock.acquire()  # held by a parent thread at fork time

        resolver.child_after_fork()

        assert resolver._state().lock.acquire(blocking=False) is True
        resolver._state().lock.release()
        assert resolver._state().entries == {}

    def test_publish_resolver_is_registered_for_fork(self) -> None:
        """A real fork while another thread holds the resolver's lock: the
        child resolves with its own state."""
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
