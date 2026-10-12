"""Tests for exception classes."""

import pytest
from celery.exceptions import (
    Ignore,
    Reject,
    Retry,
    SoftTimeLimitExceeded,
    Terminated,
    TimeLimitExceeded,
    WorkerLostError,
)
from redis.exceptions import TimeoutError as RedisTimeoutError

from stemtrace.core.exceptions import (
    ConfigurationError,
    StemtraceError,
    TransportError,
    UnsupportedBrokerError,
    is_task_interrupt,
)

TASK_INTERRUPTS = [
    SoftTimeLimitExceeded,
    TimeLimitExceeded,
    Terminated,
    WorkerLostError,
]


def test_stemtrace_error_base() -> None:
    err = StemtraceError("test error")
    assert str(err) == "test error"
    assert isinstance(err, Exception)


def test_configuration_error_inherits() -> None:
    err = ConfigurationError("bad config")
    assert isinstance(err, StemtraceError)


def test_transport_error_inherits() -> None:
    err = TransportError("connection failed")
    assert isinstance(err, StemtraceError)


def test_unsupported_broker_error() -> None:
    err = UnsupportedBrokerError("kafka")
    assert err.scheme == "kafka"
    assert "kafka" in str(err)
    assert "Unsupported broker scheme" in str(err)
    assert isinstance(err, ConfigurationError)


class _PatientSoftLimit(SoftTimeLimitExceeded):
    """A task's own subclass of Celery's soft time limit."""


class SoftTimeLimitExceeded_(Exception):
    pass


@pytest.mark.parametrize("interrupt", TASK_INTERRUPTS)
def test_is_task_interrupt_with_celery_control_flow_is_true(
    interrupt: type[Exception],
) -> None:
    assert is_task_interrupt(interrupt("limit")) is True


def test_is_task_interrupt_with_subclass_of_soft_limit_is_true() -> None:
    assert is_task_interrupt(_PatientSoftLimit()) is True


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("boom"),
        RedisTimeoutError("Timeout reading from socket"),
        TimeoutError("timed out"),
        Retry(),
        Ignore(),
        Reject(),
        SoftTimeLimitExceeded_(),
    ],
)
def test_is_task_interrupt_with_ordinary_error_is_false(error: Exception) -> None:
    assert is_task_interrupt(error) is False
