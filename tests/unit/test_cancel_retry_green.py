"""Celery's cancel-path RETRY never yields inside a gevent/eventlet signal handler.

On a cold shutdown Celery 5.6.3 cancels running tasks from inside its signal
handler (apps/worker.py ``on_cold_shutdown`` -> ``Request.cancel`` ->
``_announce_cancelled`` -> ``send_retry(einfo=None)``). CPython runs a signal
handler in whatever greenlet is current, possibly the busy task's own, which
gevent's ``terminate_job`` has just scheduled to kill (``gevent.kill``,
celery/concurrency/gevent.py). Anything in stemtrace's RETRY handling that
yields to the hub (socket I/O, a green lock) lets that kill run: GreenletExit
unwinds the handler before Celery sets ``should_terminate`` and the worker
keeps running. Each test runs a monkey-patched script in its own interpreter.
"""

import json
import subprocess
import sys
import textwrap
from importlib.util import find_spec

import pytest

NEEDS_GEVENT = pytest.mark.skipif(find_spec("gevent") is None, reason="needs gevent")
NEEDS_EVENTLET = pytest.mark.skipif(
    find_spec("eventlet") is None, reason="needs eventlet"
)

_SCRIPT = """
{patch}
import json, signal, time
from celery.app.task import Context
import stemtrace.library.signals as sig
from stemtrace.library import output_capture
from stemtrace.library.config import StemtraceConfig, set_config

set_config(StemtraceConfig(transport_url="memory://", capture_output=True))
switches = [0]


def ticker():
    # Runnable on every pass of the hub: any switch away from the handler
    # lets it count.
    while True:
        {sleep}(0)
        switches[0] += 1


class YieldingTransport:
    # A network publish: waiting on the socket yields to the hub.
    def __init__(self):
        self.events = []

    def publish(self, event):
        {sleep}(0)
        self.events.append(event.task_id)


transport = YieldingTransport()
sig.connect_signals(transport)
out = {{"handler_completed": False, "aborted_by": None, "switched_in_handler": None}}


class Task:
    name = "app.busy"


def handler(*_):
    before = switches[0]
    {kill_current}
    try:
        request = Context(id="busy-1", task="app.busy", retries=0,
                          hostname="celery@w1", delivery_info={{}})
        task = Task()
        task.request = request
        # Celery's cancel path: send_retry(task, request=..., einfo=None).
        sig._on_task_retry(sender=task, request=request, reason=None, einfo=None)
        out["handler_completed"] = True
    except BaseException as exc:
        out["aborted_by"] = type(exc).__name__
        raise
    finally:
        out["switched_in_handler"] = switches[0] != before


signal.signal(signal.SIGUSR1, handler)


def busy_task():
    output_capture.install_capture()
    output_capture.start_capture("busy-1", max_bytes=1024)
    buffer = output_capture._active_buffer.get()
    # Mid-append (the buffer's lock held), the cold shutdown lands.
    with buffer._lock:
        signal.raise_signal(signal.SIGUSR1)
    end = time.monotonic() + 0.1
    while time.monotonic() < end:
        pass


{spawn}(ticker)
{run_busy}
flush = getattr(sig, "_flush_deferred_publishes", None)
if flush is not None:
    flush(timeout_s=2)
print(json.dumps({{**out, "published": transport.events}}))
"""

_GEVENT = {
    "patch": "from gevent import monkey; monkey.patch_all(); import gevent",
    "sleep": "gevent.sleep",
    "spawn": "gevent.spawn",
    # celery 5.6.3 concurrency/gevent.py terminate_job: gevent.kill (schedules).
    "kill_current": "gevent.kill(gevent.getcurrent())",
    "run_busy": "gevent.spawn(busy_task).join(timeout=5)",
}
_EVENTLET = {
    "patch": "import eventlet; eventlet.monkey_patch()",
    "sleep": "eventlet.sleep",
    "spawn": "eventlet.spawn",
    # eventlet's kill of the current greenlet raises at once (greenthread.kill
    # throws into it), aborting Celery's handler before send_retry; only the
    # handler's own yields are under test here.
    "kill_current": "pass",
    "run_busy": "eventlet.spawn(busy_task).wait()",
}


def _run(variant: dict[str, str], template: str = _SCRIPT) -> dict[str, object]:
    script = textwrap.dedent(template.format(**variant))
    try:
        result = subprocess.run(
            [sys.executable, "-W", "ignore", "-c", script],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the signal handler hung (waited on a lock its greenlet holds)")
    assert result.returncode == 0, result.stderr[-3000:]
    parsed: dict[str, object] = json.loads(result.stdout.strip().splitlines()[-1])
    return parsed


@pytest.mark.parametrize(
    "variant",
    [
        pytest.param(_GEVENT, id="gevent", marks=NEEDS_GEVENT),
        pytest.param(_EVENTLET, id="eventlet", marks=NEEDS_EVENTLET),
    ],
)
def test_cancel_retry_never_yields_in_the_signal_handler(
    variant: dict[str, str],
) -> None:
    out = _run(variant)

    assert out["aborted_by"] is None
    assert out["handler_completed"] is True
    assert out["switched_in_handler"] is False
    # Published later, by a greenlet of its own.
    assert out["published"] == ["busy-1"]


_EXIT_SCRIPT = """
{patch}
import atexit, json
from celery.app.task import Context
from celery.exceptions import Retry
import stemtrace.library.signals as sig
from stemtrace.library.config import StemtraceConfig, set_config

set_config(StemtraceConfig(transport_url="memory://"))


class YieldingTransport:
    def __init__(self):
        self.events = []

    def publish(self, event):
        {sleep}(0)
        self.events.append(event.task_id)


transport = YieldingTransport()
sig.connect_signals(transport)
# Registered first, so it runs after stemtrace's own exit hook (LIFO).
atexit.register(lambda: print(json.dumps({{"at_exit": transport.events,
                                          "inline": inline}})))


class Task:
    name = "app.busy"


def retry(task_id, reason):
    request = Context(id=task_id, task="app.busy", retries=0,
                      hostname="celery@w1", delivery_info={{}})
    task = Task()
    task.request = request
    sig._on_task_retry(sender=task, request=request, reason=reason, einfo=None)


# A task's own retry publishes inline, before the call returns.
retry("own-retry", Retry("Retry in 5s"))
inline = list(transport.events)
# Celery's cancel path; then the interpreter exits without yielding again.
retry("cancelled", None)
"""


@pytest.mark.parametrize(
    "variant",
    [
        pytest.param(_GEVENT, id="gevent", marks=NEEDS_GEVENT),
        pytest.param(_EVENTLET, id="eventlet", marks=NEEDS_EVENTLET),
    ],
)
def test_cancel_retry_is_published_at_exit_and_other_retries_inline(
    variant: dict[str, str],
) -> None:
    out = _run(variant, _EXIT_SCRIPT)

    assert out["inline"] == ["own-retry"]
    assert out["at_exit"] == ["own-retry", "cancelled"]
