"""The DNS helper process resolves exactly like socket.getaddrinfo() in-process.

Spawns real helper processes (no network needed: names come from /etc/hosts
or fail locally). Skipped where the helper isn't used (no fork(), e.g. Windows).
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stemtrace.transports import resolver as resolver_module

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


def test_helper_usable_with_this_python() -> None:
    assert resolver_module._helper_usable() is True


def test_helper_unusable_when_executable_is_not_really_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    impostor = tmp_path / "python3"
    impostor.write_text("#!/bin/sh\necho not-python\n")
    impostor.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(impostor))

    assert resolver_module._helper_usable() is False


# Run in a fresh interpreter: it forks, and reports which lookup path the
# parent and the child used.
_FORK_SCRIPT = """\
import json, os, socket, sys, threading, warnings
from stemtrace.transports import resolver

warnings.simplefilter("ignore", DeprecationWarning)
calls = []
real_helper = resolver._getaddrinfo_in_helper
real_getaddrinfo = socket.getaddrinfo

def helper(*args):
    calls.append("helper")
    return real_helper(*args)

def in_process(*args, **kwargs):
    if threading.current_thread().name.startswith("stemtrace-dns-"):
        calls.append("in_process")
    return real_getaddrinfo(*args, **kwargs)

resolver._getaddrinfo_in_helper = helper
socket.getaddrinfo = in_process
r = resolver.PUBLISH_RESOLVER
r.resolve("localhost", 1, timeout=5.0)
before = list(calls)
read, write = os.pipe()
pid = os.fork()
if pid == 0:
    calls.clear()
    r.resolve("localhost", 2, timeout=5.0)
    os.write(write, json.dumps(calls).encode())
    os._exit(0)
os.waitpid(pid, 0)
child = json.loads(os.read(read, 1000))
for thread in threading.enumerate():
    if thread.name == "stemtrace-dns-helper-check":
        thread.join(10)
calls.clear()
r.resolve("localhost", 3, timeout=5.0)
print(json.dumps({"before": before, "child": child, "after": calls}))
"""


def test_only_a_process_that_forked_uses_the_helper() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _FORK_SCRIPT],
        capture_output=True,
        timeout=60,
        check=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )

    assert json.loads(completed.stdout) == {
        "before": ["in_process"],
        "child": ["in_process"],
        "after": ["helper"],
    }


# Forks long-lived children a few ms apart (like a prefork master starting a
# pool) while the helper check and helper lookups run, then reports what the
# resolver ended up with.
_FORK_STORM_SCRIPT = """\
import json, os, threading, time, warnings
from stemtrace.transports import resolver

warnings.simplefilter("ignore", DeprecationWarning)
r = resolver.PUBLISH_RESOLVER
r.resolve("localhost", 1, timeout=5.0)
stop = threading.Event()

def churn():
    i = 0
    while not stop.is_set():
        i += 1
        try:
            r.resolve("localhost", 1000 + i % 50, timeout=5.0)
        except OSError:
            pass
        time.sleep(0.002)

worker = threading.Thread(target=churn)
worker.start()
release_read, release_write = os.pipe()
children = []
for _ in range(150):
    pid = os.fork()
    if pid == 0:
        # Like a pool child: lives on, never execs, keeps what it inherited.
        os.close(release_write)
        os.read(release_read, 1)
        os._exit(0)
    children.append(pid)
    time.sleep(0.003)
stop.set()
worker.join()
state = r._state()
deadline = time.monotonic() + 5
while time.monotonic() < deadline and (
    state.helper_usable is None
    or any(e.lookup is not None for e in state.entries.values())
):
    time.sleep(0.1)
zombies = 0
for task in os.listdir("/proc/self/task"):
    try:  # threads and processes may come and go while this reads
        with open(f"/proc/self/task/{task}/children") as f:
            for child in f.read().split():
                with open(f"/proc/{child}/stat") as stat:
                    zombies += stat.read().split(") ")[1].startswith("Z")
    except FileNotFoundError:
        pass
result = {
    "helper_usable": state.helper_usable,
    "stuck_lookups": sum(e.lookup is not None for e in state.entries.values()),
    "zombies": zombies,
}
os.close(release_write)  # the children exit
for pid in children:
    os.waitpid(pid, 0)
print(json.dumps(result))
"""


@pytest.mark.skipif(
    not Path("/proc/self/task").is_dir(), reason="counts zombies through /proc"
)
def test_fork_storm_while_helpers_start_leaves_nothing_stuck() -> None:
    """A child forked mid-Popen() would hold the helper's pipes open: Popen()
    would never return, the check never finish, the lookup never end."""
    completed = subprocess.run(
        [sys.executable, "-c", _FORK_STORM_SCRIPT],
        capture_output=True,
        timeout=120,
        check=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )

    assert json.loads(completed.stdout) == {
        "helper_usable": True,
        "stuck_lookups": 0,
        "zombies": 0,
    }


# Under gevent, subprocess starts the helper with os.fork() from the spawning
# greenlet, which runs prepare_fork() while that greenlet holds the spawn lock.
# The same script runs under eventlet, with its monkey-patching swapped in.
_GEVENT_SCRIPT = """\
from gevent import monkey
monkey.patch_all()
import json, os, signal, threading, warnings
from stemtrace.transports import resolver

warnings.simplefilter("ignore", DeprecationWarning)
signal.alarm(30)  # a deadlock fails the test instead of hanging it
r = resolver.PUBLISH_RESOLVER
r.resolve("localhost", 1, timeout=5.0)
pid = os.fork()  # this process now forks: lookups go to the helper
if pid == 0:
    os._exit(0)
os.waitpid(pid, 0)
for thread in threading.enumerate():
    if thread.name == "stemtrace-dns-helper-check":
        thread.join(20)
addresses = r.resolve("localhost", 2, timeout=10.0)
print(json.dumps({"helper_usable": r._state().helper_usable, "resolved": bool(addresses)}))
"""


@pytest.mark.parametrize(
    "patch",
    [
        "from gevent import monkey\nmonkey.patch_all()",
        "import eventlet\neventlet.monkey_patch()",
    ],
    ids=["gevent", "eventlet"],
)
def test_helper_under_green_threads_does_not_deadlock(patch: str) -> None:
    pytest.importorskip(patch.split()[1].split(".")[0])
    script = _GEVENT_SCRIPT.replace(
        "from gevent import monkey\nmonkey.patch_all()", patch
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        timeout=60,
        check=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )

    assert json.loads(completed.stdout.splitlines()[-1]) == {
        "helper_usable": True,
        "resolved": True,
    }
