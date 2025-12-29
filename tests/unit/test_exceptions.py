"""Tests for exception classes."""

from stemtrace.core.exceptions import (
    ConfigurationError,
    StemtraceError,
    TransportError,
    UnsupportedBrokerError,
)


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
