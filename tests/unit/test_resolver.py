"""Tests for the bounded, cached host name resolver used by publishers."""

import contextlib
import os
import socket
import sys
import threading
import time
import traceback
import types
import warnings
from typing import Any, ClassVar

import pytest

from stemtrace.transports import resolver as resolver_module
from stemtrace.transports.resolver import (
    DNS_NEGATIVE_TTL,
    PUBLISH_RESOLVER,
    BoundedResolver,
)

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

    def test_failed_first_lookup_is_retried_after_negative_ttl(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        dns.answers = [socket.gaierror(socket.EAI_AGAIN, "no dns"), ADDRESS]
        resolver = _make(dns, clock)
        with pytest.raises(socket.gaierror):
            resolver.resolve("redis.local", 6379, timeout=1.0)
        clock.now += DNS_NEGATIVE_TTL

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS

    def test_failed_first_lookup_is_cached_for_negative_ttl(self) -> None:
        """A mistyped host or a dead resolver at boot: one lookup per
        DNS_NEGATIVE_TTL, not one per publish."""
        dns, clock = FakeDns(), FakeClock()
        dns.answers = [socket.gaierror(socket.EAI_NONAME, "unknown host")]
        resolver = _make(dns, clock)
        with pytest.raises(socket.gaierror):
            resolver.resolve("redis.local", 6379, timeout=1.0)

        clock.now += DNS_NEGATIVE_TTL - 0.1
        for _ in range(5):
            with pytest.raises(socket.gaierror, match="unknown host"):
                resolver.resolve("redis.local", 6379, timeout=1.0)

        assert dns.calls == ["redis.local"]

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
        clock.now += DNS_NEGATIVE_TTL

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS
        assert dns.calls == ["redis.local", "redis.local"]

    def test_invalid_host_name_fails_fast_without_lookup(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        host = "a" * 64 + ".example.com"  # DNS labels are at most 63 octets

        with pytest.raises(socket.gaierror, match="Invalid host name"):
            _make(dns, clock).resolve(host, 6379, timeout=1.0)

        assert dns.calls == []


class TestBoundedResolverLookupPath:
    """In-process lookups by default; helper processes once the process forks.

    Only a process that forks (the Celery prefork master) can hand a child a
    glibc DNS lock held by a lookup thread, so only it pays for a helper
    process per lookup; pool children and web workers resolve in-process.
    """

    @pytest.fixture
    def paths(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, FakeDns]:
        paths = {"in_process": FakeDns(), "helper": FakeDns()}
        monkeypatch.setattr(socket, "getaddrinfo", paths["in_process"])
        monkeypatch.setattr(resolver_module, "_getaddrinfo_in_helper", paths["helper"])
        monkeypatch.setattr(resolver_module, "_helper_usable", lambda: True)
        return paths

    @staticmethod
    def _fork(resolver: BoundedResolver) -> None:
        """What os.fork() runs in the parent, minus the fork itself."""
        resolver.prepare_fork()
        resolver.parent_after_fork()
        for thread in threading.enumerate():  # the one-off helper check
            if thread.name == "stemtrace-dns-helper-check":
                thread.join(JOIN_S)

    def test_lookups_run_in_process_until_the_process_forks(
        self, paths: dict[str, FakeDns]
    ) -> None:
        resolver = BoundedResolver(monotonic=FakeClock())

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS
        assert paths["in_process"].calls == ["redis.local"]
        assert paths["helper"].calls == []

    def test_lookups_after_a_fork_use_the_helper(
        self, paths: dict[str, FakeDns]
    ) -> None:
        resolver = BoundedResolver(monotonic=FakeClock())
        self._fork(resolver)

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS
        assert paths["helper"].calls == ["redis.local"]
        assert paths["in_process"].calls == []

    def test_helper_lookup_in_flight_does_not_delay_forks(
        self, paths: dict[str, FakeDns]
    ) -> None:
        resolver = BoundedResolver(monotonic=FakeClock(), fork_wait=JOIN_S)
        self._fork(resolver)
        paths["helper"].release.clear()  # stuck
        with pytest.raises(socket.gaierror):
            resolver.resolve("redis.local", 6379, timeout=0.0)
        started = time.monotonic()

        self._fork(resolver)

        assert time.monotonic() - started < 1.0
        paths["helper"].release.set()

    def test_forked_child_resolves_in_process(self, paths: dict[str, FakeDns]) -> None:
        resolver = BoundedResolver(monotonic=FakeClock())
        resolver.prepare_fork()
        resolver.child_after_fork()  # what the child runs: it hasn't forked

        resolver.resolve("redis.local", 6379, timeout=1.0)

        assert paths["in_process"].calls == ["redis.local"]
        assert paths["helper"].calls == []

    def test_unusable_helper_falls_back_in_process_with_one_warning(
        self,
        paths: dict[str, FakeDns],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(resolver_module, "_helper_usable", lambda: False)
        resolver = BoundedResolver(monotonic=FakeClock())
        self._fork(resolver)

        resolver.resolve("a.local", 6379, timeout=1.0)
        resolver.resolve("b.local", 6379, timeout=1.0)

        assert paths["in_process"].calls == ["a.local", "b.local"]
        assert paths["helper"].calls == []
        warnings_logged = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings_logged) == 1
        assert "resolving in-process" in warnings_logged[0].getMessage()


class TestHelperUsable:
    """The helper is used only where it can really run this Python."""

    @pytest.fixture(autouse=True)
    def no_spawn(self, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        spawned: list[Any] = []

        def run(*args: Any, **kwargs: Any) -> Any:
            spawned.append(args)
            raise AssertionError("helper started")

        monkeypatch.setattr(resolver_module.subprocess, "run", run)
        return spawned

    def test_frozen_application_is_unusable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)

        assert resolver_module._helper_usable() is False

    @pytest.mark.parametrize("executable", ["", "/usr/bin/uwsgi", "/opt/app/gunicorn"])
    def test_non_python_executable_is_unusable(
        self, executable: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "executable", executable)

        assert resolver_module._helper_usable() is False

    def test_no_fork_is_unusable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(os, "fork", raising=False)

        assert resolver_module._helper_usable() is False


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
    """Forking waits (boundedly) for in-process lookups, then blocks new ones.

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


class TestBoundedResolverForkWaitsForInProcessLookups:
    """Forks wait for in-process lookups (round-6 rule, kept for the fallback)."""

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


class _FakePopen:
    """subprocess.Popen stand-in that answers like a helper with no addresses."""

    held_elsewhere: ClassVar[list[bool]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        lock = resolver_module._spawn_lock()
        result: list[bool] = []
        probe = threading.Thread(target=lambda: result.append(lock.acquire(False)))
        probe.start()
        probe.join(JOIN_S)
        if result[0]:
            lock.release()
        type(self).held_elsewhere.append(not result[0])
        self.returncode = 0

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        del timeout
        return b'{"infos": []}', b""

    def kill(self) -> None:
        pass


class TestHelperSpawnVersusFork:
    """Starting a helper and forking exclude each other.

    A child forked while Popen() runs inherits the pipes Popen hasn't closed
    yet, so Popen() (or the helper's stdout) never sees EOF and the lookup or
    the check hangs. A lock around Popen(), taken before every fork, keeps
    them apart.
    """

    def test_helper_starts_under_spawn_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _FakePopen.held_elsewhere = []
        monkeypatch.setattr(resolver_module.subprocess, "Popen", _FakePopen)

        assert resolver_module._getaddrinfo_in_helper("redis.local", 1, 0, 0, 0) == []
        assert _FakePopen.held_elsewhere == [True]

    def test_prepare_fork_waits_for_helper_being_started(self) -> None:
        resolver = BoundedResolver(getaddrinfo=FakeDns(), monotonic=FakeClock())
        lock = resolver_module._spawn_lock()
        started, release = threading.Event(), threading.Event()

        def spawning() -> None:
            with lock:
                started.set()
                release.wait(JOIN_S)

        thread = threading.Thread(target=spawning)
        thread.start()
        assert started.wait(JOIN_S)
        threading.Timer(0.1, release.set).start()
        begun = time.monotonic()

        resolver.prepare_fork()
        try:
            waited = time.monotonic() - begun
        finally:
            resolver.parent_after_fork()
        thread.join(JOIN_S)

        assert waited >= 0.09
        assert lock.acquire(blocking=False) is True
        lock.release()

    def test_prepare_fork_waits_as_long_as_popen_blocks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unbounded on purpose: forking during Popen() would hand the child
        the helper's pipes (the bug the lock fixes); waiting is safe."""
        unblock = threading.Event()
        in_popen = threading.Event()

        class BlockingPopen(_FakePopen):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                in_popen.set()
                unblock.wait(JOIN_S)
                self.returncode = 0

        monkeypatch.setattr(resolver_module.subprocess, "Popen", BlockingPopen)
        spawner = threading.Thread(
            target=resolver_module._getaddrinfo_in_helper,
            args=("redis.local", 1, 0, 0, 0),
        )
        spawner.start()
        assert in_popen.wait(JOIN_S)
        resolver = BoundedResolver(getaddrinfo=FakeDns(), monotonic=FakeClock())

        def fork_hooks() -> None:
            resolver.prepare_fork()
            resolver.parent_after_fork()

        forker = threading.Thread(target=fork_hooks)

        forker.start()
        forker.join(0.3)
        still_waiting = forker.is_alive()
        unblock.set()
        forker.join(JOIN_S)
        spawner.join(JOIN_S)

        assert still_waiting is True
        assert forker.is_alive() is False

    def test_prepare_fork_from_the_spawning_thread_does_not_deadlock(self) -> None:
        """gevent's subprocess forks with os.fork() from the spawning greenlet,
        which runs the at-fork hooks while it holds the spawn lock."""
        resolver = BoundedResolver(getaddrinfo=FakeDns(), monotonic=FakeClock())
        lock = resolver_module._spawn_lock()

        with lock:
            resolver.prepare_fork()
            resolver.parent_after_fork()

        assert lock.acquire(blocking=False) is True
        lock.release()

    def test_child_gets_its_own_spawn_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parent_lock = resolver_module._spawn_lock()
        parent_lock.acquire()  # held by a parent thread at fork time
        try:
            parent_pid = os.getpid()
            monkeypatch.setattr(os, "getpid", lambda: parent_pid + 1)
            child_lock = resolver_module._spawn_lock()

            assert child_lock is not parent_lock
            assert child_lock.acquire(blocking=False) is True
            child_lock.release()
        finally:
            parent_lock.release()

    def test_helper_check_without_result_in_time_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        stuck = threading.Event()
        monkeypatch.setattr(
            resolver_module, "_helper_usable", lambda: stuck.wait(JOIN_S)
        )
        in_process, helper = FakeDns(), FakeDns()
        monkeypatch.setattr(socket, "getaddrinfo", in_process)
        monkeypatch.setattr(resolver_module, "_getaddrinfo_in_helper", helper)
        clock = FakeClock()
        resolver = BoundedResolver(monotonic=clock)
        resolver.prepare_fork()
        resolver.parent_after_fork()
        clock.now += resolver_module.HELPER_CHECK_TIMEOUT + 0.1

        resolver.resolve("redis.local", 6379, timeout=1.0)
        stuck.set()  # the check answers late: too late to switch
        for thread in threading.enumerate():
            if thread.name == "stemtrace-dns-helper-check":
                thread.join(JOIN_S)
        resolver.resolve("other.local", 6379, timeout=1.0)

        assert in_process.calls == ["redis.local", "other.local"]
        assert helper.calls == []
        assert resolver._state().helper_usable is False
        assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


class TestCachedErrorsAreRaisedFresh:
    """Re-raising one exception object grows its traceback on every raise."""

    def test_negative_cache_raises_a_new_error_each_time(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        dns.answers = [socket.gaierror(socket.EAI_NONAME, "unknown host")]
        resolver = _make(dns, clock)
        raised = []
        for _ in range(3):
            with pytest.raises(socket.gaierror) as info:
                resolver.resolve("redis.local", 6379, timeout=1.0)
            raised.append(info.value)

        assert len({id(e) for e in raised}) == 3
        assert {(e.errno, e.strerror) for e in raised} == {
            (socket.EAI_NONAME, "unknown host")
        }
        depths = [len(traceback.extract_tb(e.__traceback__)) for e in raised]
        assert depths[1] == depths[2]

    def test_waiters_get_a_new_error_not_the_cached_one(self) -> None:
        dns, clock = FakeDns(), FakeClock()
        dns.answers = [socket.gaierror(socket.EAI_NONAME, "unknown host")]
        resolver = _make(dns, clock)

        with pytest.raises(socket.gaierror) as info:
            resolver.resolve("redis.local", 6379, timeout=1.0)

        entry = resolver._state().entries[("redis.local", 6379, 0, 0, 0)]
        assert info.value is not entry.error


class TestUwsgiMasterResolvesOnCallingThread:
    """uWSGI forks in C, without Python's at-fork hooks (the master, and the
    first process of a no-master setup), so such a process must not have
    lookup threads that a fork could catch inside glibc. Workers, mules and
    spoolers don't fork and keep the bounded background lookups."""

    def _uwsgi(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        is_master: bool = False,
        no_master: bool = False,
        worker_id: int = 0,
        mule_id: int = 0,
    ) -> None:
        """Install a fake ``uwsgi`` module as uWSGI (pyuwsgi 2.0.30) shows it.

        Without ``--master`` (``no_master``), masterpid() returns 0.
        """
        fake = types.ModuleType("uwsgi")
        master_pid = os.getpid() if is_master else os.getpid() - 1
        if no_master:
            master_pid = 0
        fake.masterpid = lambda: master_pid  # type: ignore[attr-defined]
        fake.worker_id = lambda: worker_id  # type: ignore[attr-defined]
        fake.mule_id = lambda: mule_id  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "uwsgi", fake)

    def _lookup_thread(
        self, resolver: BoundedResolver, dns: "_ThreadRecordingDns"
    ) -> str:
        resolver.resolve("redis.local", 6379, timeout=1.0)
        return dns.threads[0]

    def test_uwsgi_master_resolves_on_calling_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._uwsgi(monkeypatch, is_master=True)
        dns = _ThreadRecordingDns()

        thread = self._lookup_thread(_make(dns, FakeClock()), dns)  # type: ignore[arg-type]

        assert thread == threading.current_thread().name

    @pytest.mark.parametrize(
        ("worker_id", "mule_id"),
        [(1, 0), (0, 1), (0, 0)],
        ids=["worker", "mule", "spooler"],
    )
    def test_other_uwsgi_processes_resolve_in_background(
        self, worker_id: int, mule_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mule (and a spooler) also reports worker_id 0, but isn't the master."""
        self._uwsgi(monkeypatch, is_master=False, worker_id=worker_id, mule_id=mule_id)
        dns = _ThreadRecordingDns()

        thread = self._lookup_thread(_make(dns, FakeClock()), dns)  # type: ignore[arg-type]

        assert thread.startswith("stemtrace-dns-")

    def test_no_master_first_process_resolves_on_calling_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --master, masterpid() is 0 and worker_id() 0 until it forks."""
        self._uwsgi(monkeypatch, no_master=True, worker_id=0)
        dns = _ThreadRecordingDns()

        thread = self._lookup_thread(_make(dns, FakeClock()), dns)  # type: ignore[arg-type]

        assert thread == threading.current_thread().name

    @pytest.mark.parametrize(
        ("worker_id", "mule_id"),
        [(1, 0), (2, 0), (0, 1)],
        ids=["worker-1", "worker-2", "mule"],
    )
    def test_no_master_workers_and_mules_resolve_in_background(
        self, worker_id: int, mule_id: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._uwsgi(monkeypatch, no_master=True, worker_id=worker_id, mule_id=mule_id)
        dns = _ThreadRecordingDns()

        thread = self._lookup_thread(_make(dns, FakeClock()), dns)  # type: ignore[arg-type]

        assert thread.startswith("stemtrace-dns-")

    @pytest.mark.parametrize("no_master", [False, True], ids=["master", "no-master"])
    def test_invalidate_then_resolve_stays_on_calling_thread(
        self, no_master: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """invalidate() must not start a lookup thread that a fork could catch."""
        self._uwsgi(monkeypatch, is_master=not no_master, no_master=no_master)
        dns = _ThreadRecordingDns()
        dns.answers = [ADDRESS, NEW_ADDRESS]
        resolver = _make(dns, FakeClock())  # type: ignore[arg-type]
        resolver.resolve("redis.local", 6379, timeout=1.0)

        resolver.invalidate("redis.local", 6379)

        assert [
            t.name for t in threading.enumerate() if t.name.startswith("stemtrace-dns-")
        ] == []
        entry = resolver._state().entries[("redis.local", 6379, 0, 0, 0)]
        assert entry.lookup is None
        assert resolver.resolve("redis.local", 6379, timeout=1.0) == NEW_ADDRESS
        assert dns.threads == [threading.current_thread().name] * 2

    def test_master_waits_for_lookup_already_in_flight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No second lookup for a host while one runs (e.g. started before
        uWSGI's module was importable)."""
        self._uwsgi(monkeypatch, is_master=True)
        dns = FakeDns()
        resolver = _make(dns, FakeClock())
        state = resolver._state()
        entry = state.entries.setdefault(
            ("redis.local", 6379, 0, 0, 0), resolver_module._Entry()
        )
        entry.lookup = threading.Event()

        with pytest.raises(socket.gaierror, match="Timed out") as info:
            resolver.resolve("redis.local", 6379, timeout=0.05)

        assert info.value.errno == socket.EAI_AGAIN
        assert dns.calls == []

    def test_master_serves_cached_answer_while_lookup_in_flight(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._uwsgi(monkeypatch, is_master=True)
        dns, clock = FakeDns(), FakeClock()
        resolver = _make(dns, clock)
        resolver.resolve("redis.local", 6379, timeout=1.0)
        entry = resolver._state().entries[("redis.local", 6379, 0, 0, 0)]
        entry.lookup = threading.Event()
        clock.now += 31.0  # past the TTL

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS
        assert dns.calls == ["redis.local"]

    def test_master_cache_hit_needs_no_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._uwsgi(monkeypatch, is_master=True)
        dns, clock = FakeDns(), FakeClock()
        resolver = _make(dns, clock)
        resolver.resolve("redis.local", 6379, timeout=1.0)
        clock.now += 29.0

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS
        assert dns.calls == ["redis.local"]

    def test_master_failed_lookup_is_cached_as_new_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._uwsgi(monkeypatch, is_master=True)
        dns, clock = FakeDns(), FakeClock()
        dns.answers = [socket.gaierror(socket.EAI_NONAME, "unknown host")]
        resolver = _make(dns, clock)
        raised = []
        for _ in range(3):
            with pytest.raises(socket.gaierror, match="unknown host") as info:
                resolver.resolve("redis.local", 6379, timeout=1.0)
            raised.append(info.value)

        assert dns.calls == ["redis.local"]
        assert len({id(e) for e in raised}) == 3

    def test_master_refresh_failure_falls_back_to_cached_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._uwsgi(monkeypatch, is_master=True)
        dns, clock = FakeDns(), FakeClock()
        dns.answers = [ADDRESS, socket.gaierror(socket.EAI_AGAIN, "no dns")]
        resolver = _make(dns, clock)
        resolver.resolve("redis.local", 6379, timeout=1.0)
        clock.now += 31.0  # past the TTL: refreshes on the calling thread

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS
        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS
        assert dns.calls == ["redis.local", "redis.local"]

    def test_master_unexpected_lookup_error_becomes_gaierror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._uwsgi(monkeypatch, is_master=True)
        dns, clock = FakeDns(), FakeClock()
        dns.answers = [UnicodeError("label too long")]

        with pytest.raises(socket.gaierror, match="label too long") as info:
            _make(dns, clock).resolve("redis.local", 6379, timeout=1.0)

        assert isinstance(info.value.__cause__, UnicodeError)


class _ThreadRecordingDns(FakeDns):
    """FakeDns that also records which thread each lookup ran on."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: list[str] = []

    def __call__(self, host: str, port: int, *args: Any) -> list[AddrInfo]:
        self.threads.append(threading.current_thread().name)
        return super().__call__(host, port, *args)
