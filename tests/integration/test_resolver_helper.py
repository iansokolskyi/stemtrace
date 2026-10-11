"""The DNS helper process resolves exactly like socket.getaddrinfo() in-process.

Spawns real helper processes (no network needed: names come from /etc/hosts
or fail locally). Skipped where the helper isn't used (no fork(), e.g. Windows).
"""

import os
import socket
import time
from pathlib import Path

import pytest

from stemtrace.transports import resolver as resolver_module
from stemtrace.transports.resolver import BoundedResolver

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not hasattr(os, "fork"), reason="helper needs fork()"),
]


@pytest.mark.parametrize(
    ("family", "socktype", "proto"),
    [
        (0, 0, 0),
        (0, socket.SOCK_STREAM, 0),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP),
        (socket.AF_INET6, 0, 0),
    ],
)
def test_helper_matches_in_process_getaddrinfo(
    family: int, socktype: int, proto: int
) -> None:
    try:
        expected = socket.getaddrinfo("localhost", 6379, family, socktype, proto)
    except socket.gaierror as exc:
        pytest.skip(f"no such address on this host: {exc}")

    result = resolver_module._getaddrinfo_in_helper(
        "localhost", 6379, family, socktype, proto
    )

    assert result == expected
    assert [type(r[0]) for r in result] == [type(r[0]) for r in expected]


def test_helper_reports_same_error_as_in_process() -> None:
    host = "no-such-host.invalid"
    with pytest.raises(socket.gaierror) as expected:
        socket.getaddrinfo(host, 6379)

    with pytest.raises(socket.gaierror) as raised:
        resolver_module._getaddrinfo_in_helper(host, 6379, 0, 0, 0)

    assert raised.value.errno == expected.value.errno


def test_helper_never_goes_through_a_shell(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    host = f"x;touch {marker};$(touch {marker})"

    with pytest.raises(socket.gaierror):
        resolver_module._getaddrinfo_in_helper(host, 6379, 0, 0, 0)

    assert not marker.exists()


def test_helper_killed_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        resolver_module, "_HELPER_SCRIPT", "import time; time.sleep(60)"
    )
    monkeypatch.setattr(resolver_module, "HELPER_TIMEOUT", 0.5)
    started = time.monotonic()

    with pytest.raises(socket.gaierror, match="timed out"):
        resolver_module._getaddrinfo_in_helper("localhost", 6379, 0, 0, 0)

    # subprocess.run() kills the helper on timeout and reaps it.
    assert time.monotonic() - started < 5.0


def test_cold_helper_resolves_well_within_publish_bound() -> None:
    """Spawning a helper must stay far below the 2s connect timeout."""
    durations = []
    for _ in range(5):
        started = time.monotonic()
        resolver_module._getaddrinfo_in_helper("localhost", 6379, 0, 0, 0)
        durations.append(time.monotonic() - started)

    assert max(durations) < 1.0, durations


def test_resolver_by_default_resolves_through_helper() -> None:
    resolver = BoundedResolver()

    result = resolver.resolve(
        "localhost", 6379, socktype=socket.SOCK_STREAM, timeout=2.0
    )

    assert result == socket.getaddrinfo("localhost", 6379, 0, socket.SOCK_STREAM)
