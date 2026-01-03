"""Tests for public API."""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI

import stemtrace
from stemtrace import (
    ConfigurationError,
    StemtraceConfig,
    __version__,
    _reset,
    get_config,
    get_transport,
    init_app,
    init_worker,
    is_initialized,
)
from stemtrace.library.signals import disconnect_signals
from stemtrace.library.transports.memory import MemoryTransport


@pytest.fixture(autouse=True)
def cleanup() -> None:
    """Clean up after each test."""
    yield
    disconnect_signals()
    MemoryTransport.clear()
    _reset()  # Reset module-level transport state


def test_version() -> None:
    """Version is set."""
    assert __version__ == "0.1.1"


class TestInit:
    """Tests for init_worker() function."""

    def test_init_worker_with_explicit_transport_url(self) -> None:
        """init_worker() works with explicit transport_url."""
        app = MagicMock()
        app.conf.broker_url = None

        init_worker(app, transport_url="memory://")

        config = get_config()
        assert config is not None
        assert config.transport_url == "memory://"

    def test_init_worker_uses_celery_broker_url(self) -> None:
        """init_worker() falls back to Celery's broker_url."""
        app = MagicMock()
        app.conf.broker_url = "memory://"

        init_worker(app)

        config = get_config()
        assert config is not None
        assert config.transport_url == "memory://"

    def test_init_worker_raises_without_broker_url(self) -> None:
        """init_worker() raises ConfigurationError if no broker URL available."""
        app = MagicMock()
        app.conf.broker_url = None

        with pytest.raises(ConfigurationError) as exc_info:
            init_worker(app)

        assert "No broker URL" in str(exc_info.value)

    def test_init_worker_stores_config(self) -> None:
        """init_worker() stores configuration for later retrieval."""
        app = MagicMock()

        init_worker(
            app,
            transport_url="memory://",
            prefix="custom_prefix",
            ttl=3600,
            capture_args=False,
            scrub_sensitive_data=False,
        )

        config = get_config()
        assert config is not None
        assert config.prefix == "custom_prefix"
        assert config.ttl == 3600
        assert config.capture_args is False
        assert config.scrub_sensitive_data is False

    def test_namespace_style_init(self) -> None:
        """init_worker() can be called via namespace."""
        app = MagicMock()
        app.conf.broker_url = "memory://"

        stemtrace.init_worker(app)

        assert stemtrace.is_initialized() is True
        assert stemtrace.get_config() is not None

    def test_init_app_raises_without_broker_url_and_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """init_app() should require broker_url or STEMTRACE_BROKER_URL."""
        monkeypatch.delenv("STEMTRACE_BROKER_URL", raising=False)
        app = FastAPI()

        with pytest.raises(ConfigurationError) as exc_info:
            init_app(app, broker_url=None, embedded_consumer=False, serve_ui=False)

        assert "No broker URL available" in str(exc_info.value)


class TestIntrospection:
    """Tests for introspection functions."""

    def test_is_initialized_false_before_init_worker(self) -> None:
        """is_initialized() returns False before init_worker()."""
        assert is_initialized() is False

    def test_is_initialized_true_after_init_worker(self) -> None:
        """is_initialized() returns True after init_worker()."""
        app = MagicMock()
        app.conf.broker_url = "memory://"

        init_worker(app)

        assert is_initialized() is True

    def test_get_config_none_before_init_worker(self) -> None:
        """get_config() returns None before init_worker()."""
        assert get_config() is None

    def test_get_config_returns_config_after_init_worker(self) -> None:
        """get_config() returns StemtraceConfig after init_worker()."""
        app = MagicMock()

        init_worker(app, transport_url="memory://", prefix="test_prefix")

        config = get_config()
        assert config is not None
        assert isinstance(config, StemtraceConfig)
        assert config.prefix == "test_prefix"

    def test_get_transport_none_before_init_worker(self) -> None:
        """get_transport() returns None before init_worker()."""
        assert get_transport() is None

    def test_get_transport_returns_transport_after_init_worker(self) -> None:
        """get_transport() returns EventTransport after init_worker()."""
        app = MagicMock()

        init_worker(app, transport_url="memory://")

        transport = get_transport()
        assert transport is not None
        # MemoryTransport is what we get for memory://
        assert isinstance(transport, MemoryTransport)

    def test_exports_in_all(self) -> None:
        """New functions are exported in __all__."""
        assert "is_initialized" in stemtrace.__all__
        assert "get_config" in stemtrace.__all__
        assert "get_transport" in stemtrace.__all__
        assert "StemtraceConfig" in stemtrace.__all__
