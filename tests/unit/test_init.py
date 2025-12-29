"""Tests for public API."""

from unittest.mock import MagicMock

import pytest

import celery_flow
from celery_flow import (
    CeleryFlowConfig,
    ConfigurationError,
    __version__,
    _reset,
    get_config,
    get_transport,
    init,
    is_initialized,
)
from celery_flow.library.signals import disconnect_signals
from celery_flow.library.transports.memory import MemoryTransport


@pytest.fixture(autouse=True)
def cleanup() -> None:
    """Clean up after each test."""
    yield
    disconnect_signals()
    MemoryTransport.clear()
    _reset()  # Reset module-level transport state


def test_version() -> None:
    """Version is set."""
    assert __version__ == "0.1.0"


class TestInit:
    """Tests for init() function."""

    def test_init_with_explicit_transport_url(self) -> None:
        """init() works with explicit transport_url."""
        app = MagicMock()
        app.conf.broker_url = None

        init(app, transport_url="memory://")

        config = get_config()
        assert config is not None
        assert config.transport_url == "memory://"

    def test_init_uses_celery_broker_url(self) -> None:
        """init() falls back to Celery's broker_url."""
        app = MagicMock()
        app.conf.broker_url = "memory://"

        init(app)

        config = get_config()
        assert config is not None
        assert config.transport_url == "memory://"

    def test_init_raises_without_broker_url(self) -> None:
        """init() raises ConfigurationError if no broker URL available."""
        app = MagicMock()
        app.conf.broker_url = None

        with pytest.raises(ConfigurationError) as exc_info:
            init(app)

        assert "No broker URL" in str(exc_info.value)

    def test_init_stores_config(self) -> None:
        """init() stores configuration for later retrieval."""
        app = MagicMock()

        init(
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
        """init() can be called via namespace (Sentry-style)."""
        app = MagicMock()
        app.conf.broker_url = "memory://"

        # Sentry-style: celery_flow.init(app)
        celery_flow.init(app)

        assert celery_flow.is_initialized() is True
        assert celery_flow.get_config() is not None


class TestIntrospection:
    """Tests for introspection functions."""

    def test_is_initialized_false_before_init(self) -> None:
        """is_initialized() returns False before init()."""
        assert is_initialized() is False

    def test_is_initialized_true_after_init(self) -> None:
        """is_initialized() returns True after init()."""
        app = MagicMock()
        app.conf.broker_url = "memory://"

        init(app)

        assert is_initialized() is True

    def test_get_config_none_before_init(self) -> None:
        """get_config() returns None before init()."""
        assert get_config() is None

    def test_get_config_returns_config_after_init(self) -> None:
        """get_config() returns CeleryFlowConfig after init()."""
        app = MagicMock()

        init(app, transport_url="memory://", prefix="test_prefix")

        config = get_config()
        assert config is not None
        assert isinstance(config, CeleryFlowConfig)
        assert config.prefix == "test_prefix"

    def test_get_transport_none_before_init(self) -> None:
        """get_transport() returns None before init()."""
        assert get_transport() is None

    def test_get_transport_returns_transport_after_init(self) -> None:
        """get_transport() returns EventTransport after init()."""
        app = MagicMock()

        init(app, transport_url="memory://")

        transport = get_transport()
        assert transport is not None
        # MemoryTransport is what we get for memory://
        assert isinstance(transport, MemoryTransport)

    def test_exports_in_all(self) -> None:
        """New functions are exported in __all__."""
        assert "is_initialized" in celery_flow.__all__
        assert "get_config" in celery_flow.__all__
        assert "get_transport" in celery_flow.__all__
        assert "CeleryFlowConfig" in celery_flow.__all__
