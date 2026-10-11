"""Tests for the bounded, cached host name resolver used by publishers."""

import os
import socket
import threading
from typing import Any

import pytest

from stemtrace.transports.resolver import BoundedResolver

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
        resolver._lock.acquire()  # held by a parent thread at fork time
        parent_pid = os.getpid()

        monkeypatch.setattr(os, "getpid", lambda: parent_pid + 1)

        assert resolver.resolve("redis.local", 6379, timeout=1.0) == ADDRESS
        assert dns.calls == ["redis.local", "redis.local"]
