"""Tests for public API."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
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
def cleanup() -> object:
    """Clean up after each test."""
    yield
    disconnect_signals()
    MemoryTransport.clear()
    _reset()  # Reset module-level transport state


def test_version() -> None:
    """Version is set and matches installed package metadata."""
    try:
        installed = package_version("stemtrace")
    except PackageNotFoundError as exc:
        raise AssertionError("stemtrace package metadata not available") from exc
    assert __version__ == installed


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
        monkeypatch.delenv("STEMTRACE_TRANSPORT_URL", raising=False)
        app = FastAPI()

        with pytest.raises(ConfigurationError) as exc_info:
            init_app(app, broker_url=None, embedded_consumer=False, serve_ui=False)

        assert "No broker URL available" in str(exc_info.value)

    def test_init_app_transport_url_defaults_to_broker_url(self) -> None:
        """transport_url defaults to broker_url when not provided."""
        app = FastAPI()
        ext = init_app(
            app,
            broker_url="memory://",
            embedded_consumer=False,
            serve_ui=False,
        )
        assert ext is not None
        # We don't expose transport_url publicly; this verifies it doesn't error
        # when transport_url is omitted and broker_url is provided.

    def test_init_app_passes_explicit_transport_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit transport_url is passed through to the extension."""
        captured: dict[str, object] = {}

        class _FakeExtension:
            def __init__(
                self, broker_url: str, *, transport_url: str | None = None, **_: object
            ) -> None:
                captured["broker_url"] = broker_url
                captured["transport_url"] = transport_url

            def init_app(self, _app: FastAPI, *, prefix: str | None = None) -> None:
                del prefix
                return None

        monkeypatch.setattr(stemtrace, "StemtraceExtension", _FakeExtension)

        app = FastAPI()
        init_app(
            app,
            broker_url="amqp://guest:guest@localhost:5672//",
            transport_url="redis://localhost:6379/0",
            embedded_consumer=False,
            serve_ui=False,
        )

        assert captured["broker_url"] == "amqp://guest:guest@localhost:5672//"
        assert captured["transport_url"] == "redis://localhost:6379/0"

    def test_init_app_uses_transport_url_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """STEMTRACE_TRANSPORT_URL overrides broker_url for event consumption."""

        captured: dict[str, object] = {}

        class _FakeExtension:
            def __init__(
                self,
                broker_url: str,
                *,
                transport_url: str | None = None,
                embedded_consumer: bool = True,
                serve_ui: bool = True,
                prefix: str = "/stemtrace",
                ttl: int = 86400,
                max_nodes: int = 10000,
                auth_dependency: object = None,
            ) -> None:
                captured["broker_url"] = broker_url
                captured["transport_url"] = transport_url
                del embedded_consumer, serve_ui, prefix, ttl, max_nodes, auth_dependency

            def init_app(self, _app: FastAPI, *, prefix: str | None = None) -> None:
                del prefix
                return None

        monkeypatch.setenv(
            "STEMTRACE_BROKER_URL", "amqp://guest:guest@localhost:5672//"
        )
        monkeypatch.setenv("STEMTRACE_TRANSPORT_URL", "redis://localhost:6379/0")
        monkeypatch.setattr(stemtrace, "StemtraceExtension", _FakeExtension)

        app = FastAPI()
        init_app(app, broker_url=None, embedded_consumer=False, serve_ui=False)

        assert captured["broker_url"] == "amqp://guest:guest@localhost:5672//"
        assert captured["transport_url"] == "redis://localhost:6379/0"


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
