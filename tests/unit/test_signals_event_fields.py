"""exception_type, hostname, queue and routing_key on the events the handlers publish.

Requests are Celery's own ``Context`` (what ``task.request`` is in a worker),
built with the fields Celery 5.6.3's ``Request.__init__``
(celery/worker/request.py) gives it: ``hostname`` is the consumer's nodename
and ``delivery_info`` holds the ``exchange`` and ``routing_key`` the broker
reported.
"""

from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from celery import Celery, Task
from celery.app.task import Context
from celery.exceptions import Retry
from celery.signals import task_failure, task_retry
from celery.utils.serialization import UnpickleableExceptionWrapper
from kombu import Exchange, Queue

from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.library.bootsteps import ReceivedEventStep, _set_publisher
from stemtrace.library.config import StemtraceConfig, set_config
from stemtrace.library.signals import (
    _destination,
    _exception_type,
    _get_hostname_and_pid,
    _on_task_failure,
    _on_task_postrun,
    _on_task_prerun,
    _on_task_retry,
    _on_task_revoked,
    connect_signals,
    disconnect_signals,
)
from stemtrace.transports.memory import MemoryTransport

NODENAME = "celery@media-gpu-1"


class PartnerAPIError(Exception):
    """A user-defined exception, as a task module would declare it."""


class Outer:
    """Holds a nested exception class."""

    class NestedError(Exception):
        """An exception class defined inside a class."""


@pytest.fixture(autouse=True)
def transport() -> Iterator[MemoryTransport]:
    """Connect the handlers to a fresh memory transport."""
    MemoryTransport.clear()
    set_config(StemtraceConfig(transport_url="memory://"))
    transport = MemoryTransport()
    connect_signals(transport)
    yield transport
    disconnect_signals()
    _set_publisher(None)
    MemoryTransport.clear()


def _request(
    *,
    exchange: Any = "",
    routing_key: Any = "media",
    hostname: Any = NODENAME,
    **fields: Any,
) -> Context:
    """A worker-side task request, as Celery builds it."""
    return Context(
        id="task-1",
        task="app.encode",
        retries=0,
        hostname=hostname,
        delivery_info={
            "exchange": exchange,
            "routing_key": routing_key,
            "priority": None,
            "redelivered": False,
        },
        **fields,
    )


class _Task:
    """The parts of a Celery task the handlers read (hashable, as a signal sender)."""

    name = "app.encode"

    def __init__(self, request: Context) -> None:
        """Run under ``request``."""
        self.request = request


def _task(request: Context) -> _Task:
    return _Task(request)


def _prerun(request: Context) -> None:
    _on_task_prerun(task_id="task-1", task=_task(request), args=(), kwargs={})


def _postrun(request: Context) -> None:
    _on_task_postrun(
        task_id="task-1",
        task=_task(request),
        args=(),
        kwargs={},
        retval=1,
        state="SUCCESS",
    )


def _failure(request: Context) -> None:
    _on_task_failure(
        task_id="task-1",
        exception=ValueError("boom"),
        args=(),
        kwargs={},
        traceback=None,
        einfo=None,
        sender=_task(request),
    )


def _retry(request: Context) -> None:
    _on_task_retry(
        sender=_task(request), request=request, reason=Retry(exc=ValueError("x"))
    )


def _revoked(request: Context) -> None:
    _on_task_revoked(
        request=request,
        terminated=False,
        signum=None,
        expired=False,
        sender=_task(request),
    )


WORKER_HANDLERS: dict[str, Callable[[Context], None]] = {
    "started": _prerun,
    "success": _postrun,
    "failure": _failure,
    "retry": _retry,
    "revoked": _revoked,
}


def _only_event() -> TaskEvent:
    [event] = MemoryTransport.events
    assert isinstance(event, TaskEvent)
    return event


class TestExceptionType:
    """exception_type names the class, never the message."""

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ValueError("boom"), "ValueError"),
            (KeyError("k"), "KeyError"),
            (PartnerAPIError("502"), f"{__name__}.PartnerAPIError"),
            (Outer.NestedError(), f"{__name__}.Outer.NestedError"),
            (Retry(), "celery.exceptions.Retry"),
            (
                UnpickleableExceptionWrapper(
                    "myapp.errors", "PartnerAPIError", ("502",), "502"
                ),
                "myapp.errors.PartnerAPIError",
            ),
            (None, None),
        ],
        ids=["builtin", "builtin-key", "module", "nested", "celery", "wrapped", "none"],
    )
    def test_exception_type_names_class(
        self, exc: BaseException | None, expected: str | None
    ) -> None:
        assert _exception_type(exc) == expected

    def test_failure_event_carries_exception_type_without_message(self) -> None:
        _on_task_failure(
            task_id="task-1",
            exception=PartnerAPIError("token=sk_live_secret rejected"),
            args=(),
            kwargs={},
            traceback=None,
            einfo=None,
            sender=_task(_request()),
        )

        event = _only_event()
        assert event.exception_type == f"{__name__}.PartnerAPIError"
        assert event.exception == "PartnerAPIError: token=sk_live_secret rejected"

    def test_failure_sent_by_celery_carries_exception_type(self) -> None:
        # Mirrors celery 5.6.3 app/trace.py TraceInfo.handle_failure.
        task_failure.send(
            sender=_task(_request()),
            task_id="task-1",
            exception=KeyError("k"),
            args=(),
            kwargs={},
            traceback=None,
            einfo=None,
        )

        assert _only_event().exception_type == "KeyError"

    def test_retry_event_names_the_cause_not_retry(self) -> None:
        # celery 5.6.3 app/trace.py handle_retry sends reason=the Retry raised
        # by task.retry(exc=...), whose .exc is the exception that caused it.
        request = _request()
        task_retry.send(
            sender=_task(request),
            request=request,
            reason=Retry("Retry in 5s", exc=PartnerAPIError("502")),
            einfo=None,
        )

        assert _only_event().exception_type == f"{__name__}.PartnerAPIError"

    def test_retry_raised_inside_except_names_the_handled_exception(self) -> None:
        # `except E: raise self.retry()` passes no exc: celery 5.6.3 app/task.py
        # Task.retry raises Retry(exc=None) while E is handled, so E is only
        # chained as the Retry's __context__.
        app = Celery("retry-tests", broker="memory://")

        @app.task(name="app.flaky", bind=True, max_retries=1, default_retry_delay=0)
        def flaky(self: Task) -> None:
            """Retry once from inside an except block."""
            try:
                raise ConnectionError("refused")
            except ConnectionError:
                if self.request.retries == 0:
                    # retry() raises its Retry itself; no `from` clause ever applies.
                    raise self.retry()  # noqa: B904

        flaky.apply()

        assert [
            (e.state, e.exception_type)
            for e in MemoryTransport.events
            if e.state in (TaskState.RETRY, TaskState.SUCCESS)
        ] == [(TaskState.RETRY, "ConnectionError"), (TaskState.SUCCESS, None)]

    def test_retry_with_suppressed_context_is_celery_retry(self) -> None:
        # What `raise Retry() from None` inside an except block leaves.
        retry = Retry()
        retry.__context__ = ConnectionError("refused")
        retry.__suppress_context__ = True
        request = _request()
        _on_task_retry(sender=_task(request), request=request, reason=retry)

        assert _only_event().exception_type == "celery.exceptions.Retry"

    def test_retry_cause_comes_before_context(self) -> None:
        # As traceback.py picks them: the cause even when the context isn't
        # suppressed (assigning __cause__ suppresses it; undo that).
        retry = Retry()
        retry.__context__ = ConnectionError("refused")
        retry.__cause__ = PartnerAPIError("502")
        retry.__suppress_context__ = False
        request = _request()
        _on_task_retry(sender=_task(request), request=request, reason=retry)

        assert _only_event().exception_type == f"{__name__}.PartnerAPIError"

    def test_retry_with_explicit_cause_names_it(self) -> None:
        retry = Retry()
        retry.__cause__ = PartnerAPIError("502")
        request = _request()
        _on_task_retry(sender=_task(request), request=request, reason=retry)

        assert _only_event().exception_type == f"{__name__}.PartnerAPIError"

    def test_retry_without_cause_is_celery_retry(self) -> None:
        request = _request()
        _on_task_retry(sender=_task(request), request=request, reason=Retry())

        assert _only_event().exception_type == "celery.exceptions.Retry"

    def test_retry_of_plain_exception_names_it(self) -> None:
        request = _request()
        _on_task_retry(sender=_task(request), request=request, reason=TimeoutError())

        assert _only_event().exception_type == "TimeoutError"

    @pytest.mark.parametrize("reason", ["Max retries exceeded", None])
    def test_retry_without_exception_has_no_exception_type(self, reason: Any) -> None:
        # A string reason, or Celery's cancel path (no reason, no einfo).
        request = _request()
        _on_task_retry(sender=_task(request), request=request, reason=reason)

        event = _only_event()
        assert event.exception is not None
        assert event.exception_type is None

    def test_success_has_no_exception_type(self) -> None:
        _postrun(_request())

        assert _only_event().exception_type is None


class TestHostname:
    """hostname is the worker's nodename, the one the Workers page lists."""

    @pytest.mark.parametrize("handler", WORKER_HANDLERS.values(), ids=WORKER_HANDLERS)
    def test_worker_event_carries_nodename(
        self, handler: Callable[[Context], None]
    ) -> None:
        handler(_request())

        assert _only_event().hostname == NODENAME

    def test_hostname_matches_worker_event_hostname(self) -> None:
        # The worker_ready event and task events read the same attribute.
        worker = SimpleNamespace(hostname=NODENAME)
        _prerun(_request(hostname=worker.hostname))

        assert _only_event().hostname == _get_hostname_and_pid(worker)[0]

    def test_eager_task_has_no_hostname(self) -> None:
        # celery 5.6.3 Task.apply gives eager requests the bare gethostname(),
        # not a nodename: no worker ran the task.
        _prerun(_request(hostname="build-box", is_eager=True))

        assert _only_event().hostname is None

    @pytest.mark.parametrize("hostname", [None, "", 42])
    def test_missing_or_non_string_hostname_is_none(self, hostname: Any) -> None:
        _prerun(_request(hostname=hostname))

        assert _only_event().hostname is None

    def test_failure_from_worker_main_process_has_no_hostname(self) -> None:
        # celery 5.6.3 worker/request.py Request.on_failure sends task_failure
        # from the main process (hard time limit, lost worker), where
        # task.request is an empty Context.
        _failure(Context())

        event = _only_event()
        assert (event.hostname, event.queue, event.routing_key) == (None, None, None)

    def test_pending_has_no_hostname(self, transport: MemoryTransport) -> None:
        app = Celery("hostname-tests", broker="memory://")
        app.send_task("app.encode")

        [event] = MemoryTransport.events
        assert event.state == TaskState.PENDING
        assert event.hostname is None


class TestDestination:
    """queue and routing_key from the exchange and routing key Celery used."""

    @pytest.mark.parametrize(
        ("exchange", "routing_key", "expected"),
        [
            ("", "media", ("media", "media")),
            (Exchange(""), "media", ("media", "media")),
            ("tasks", "media.video", (None, "media.video")),
            (Exchange("tasks", type="topic"), "media.video", (None, "media.video")),
            (None, "media", (None, "media")),
            ("", None, (None, None)),
            ("", "", (None, None)),
            (None, None, (None, None)),
            ("", 7, (None, None)),
        ],
        ids=[
            "anon",
            "anon-exchange-object",
            "named",
            "named-exchange-object",
            "unknown-exchange",
            "no-key",
            "empty-key",
            "nothing",
            "non-string-key",
        ],
    )
    def test_destination(
        self, exchange: Any, routing_key: Any, expected: tuple[str | None, ...]
    ) -> None:
        assert _destination(exchange, routing_key) == expected

    @pytest.mark.parametrize("handler", WORKER_HANDLERS.values(), ids=WORKER_HANDLERS)
    def test_worker_event_carries_queue_from_delivery_info(
        self, handler: Callable[[Context], None]
    ) -> None:
        handler(_request(exchange="", routing_key="media"))

        event = _only_event()
        assert (event.queue, event.routing_key) == ("media", "media")

    def test_worker_event_through_named_exchange_has_only_routing_key(self) -> None:
        _prerun(_request(exchange="tasks", routing_key="media.video"))

        event = _only_event()
        assert (event.queue, event.routing_key) == (None, "media.video")

    @pytest.mark.parametrize("delivery_info", [None, "not-a-dict"])
    def test_worker_event_without_delivery_info_has_no_destination(
        self, delivery_info: Any
    ) -> None:
        request = _request()
        request.delivery_info = delivery_info
        _prerun(request)

        event = _only_event()
        assert (event.queue, event.routing_key) == (None, None)


class TestPublishedDestination:
    """PENDING carries where Celery itself published the message (memory broker)."""

    @pytest.fixture
    def app(self) -> Celery:
        app = Celery("destination-tests", broker="memory://")

        @app.task(name="app.encode")
        def encode() -> None:
            """Do nothing."""

        return app

    def _pending(self) -> TaskEvent:
        [event] = MemoryTransport.events
        assert event.state == TaskState.PENDING
        return event

    def test_default_route_is_default_queue(self, app: Celery) -> None:
        app.tasks["app.encode"].delay()

        event = self._pending()
        assert (event.queue, event.routing_key) == ("celery", "celery")

    def test_task_routes_queue(self, app: Celery) -> None:
        app.conf.task_routes = {"app.encode": {"queue": "media"}}
        app.tasks["app.encode"].delay()

        event = self._pending()
        assert (event.queue, event.routing_key) == ("media", "media")

    def test_apply_async_queue(self, app: Celery) -> None:
        app.tasks["app.encode"].apply_async(queue="media")

        event = self._pending()
        assert (event.queue, event.routing_key) == ("media", "media")

    def test_route_naming_exchange_and_key_has_only_routing_key(
        self, app: Celery
    ) -> None:
        # A direct exchange, but the route names both: Celery publishes to it.
        app.conf.task_queues = [
            Queue("media", Exchange("tasks", type="direct"), routing_key="media.video")
        ]
        app.conf.task_routes = {
            "app.encode": {
                "queue": "media",
                "exchange": "tasks",
                "routing_key": "media.video",
            }
        }
        app.tasks["app.encode"].delay()

        event = self._pending()
        assert (event.queue, event.routing_key) == (None, "media.video")

    def test_fanout_exchange_has_no_queue(self, app: Celery) -> None:
        app.conf.task_queues = [Queue("media", Exchange("broadcast", type="fanout"))]
        app.conf.task_routes = {"app.encode": {"queue": "media"}}
        app.tasks["app.encode"].delay()

        assert self._pending().queue is None

    def test_eager_apply_has_no_hostname_or_destination(self, app: Celery) -> None:
        # celery 5.6.3 Task.apply: hostname is the bare gethostname() and
        # delivery_info has no exchange or routing key.
        app.tasks["app.encode"].apply()

        assert [
            (e.state, e.hostname, e.queue, e.routing_key)
            for e in MemoryTransport.events
        ] == [
            (TaskState.STARTED, None, None, None),
            (TaskState.SUCCESS, None, None, None),
        ]

    def test_topic_exchange_has_only_routing_key(self, app: Celery) -> None:
        app.conf.task_queues = [
            Queue("media", Exchange("tasks", type="topic"), routing_key="media.#")
        ]
        app.conf.task_routes = {
            "app.encode": {"queue": "media", "routing_key": "media.video"}
        }
        app.tasks["app.encode"].delay()

        event = self._pending()
        assert (event.queue, event.routing_key) == (None, "media.video")


class TestReceived:
    """RECEIVED carries the consumer's nodename and the message's destination."""

    def _receive(self, consumer: Any, delivery_info: Any) -> TaskEvent:
        events: list[TaskEvent] = []
        _set_publisher(events.append)
        consumer.strategies = {"app.encode": lambda *_a, **_k: None}
        ReceivedEventStep(consumer).start(consumer)
        message = SimpleNamespace(
            headers={"id": "task-1", "task": "app.encode"},
            delivery_info=delivery_info,
        )
        consumer.strategies["app.encode"](message, None, None, None, None)
        [event] = events
        return event

    def test_received_carries_hostname_queue_and_routing_key(self) -> None:
        event = self._receive(
            SimpleNamespace(hostname=NODENAME),
            {"exchange": "", "routing_key": "media", "redelivered": False},
        )

        assert event.state == TaskState.RECEIVED
        assert (event.hostname, event.queue, event.routing_key) == (
            NODENAME,
            "media",
            "media",
        )

    def test_received_without_delivery_info_or_hostname(self) -> None:
        event = self._receive(SimpleNamespace(), None)

        assert (event.hostname, event.queue, event.routing_key) == (None, None, None)
