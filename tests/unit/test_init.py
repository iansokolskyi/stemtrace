"""Tests for public API."""

from unittest.mock import MagicMock

import pytest

from celery_flow import ConfigurationError, __version__, init
from celery_flow.library.config import get_config
from celery_flow.library.signals import disconnect_signals
from celery_flow.library.transports.memory import MemoryTransport


@pytest.fixture(autouse=True)
def cleanup() -> None:
    """Clean up after each test."""
    yield
    disconnect_signals()
    MemoryTransport.clear()


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
