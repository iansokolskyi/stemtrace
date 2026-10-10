"""Tests for public API."""

import logging
import warnings
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

_INIT_LOGGER = "stemtrace"


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

    def test_init_worker_output_capture_is_off_by_default(self) -> None:
        """Output capture is opt-in, with a 16 KB per-attempt cap."""
        init_worker(MagicMock(), transport_url="memory://")

        config = get_config()
        assert config is not None
        assert config.capture_output is False
        assert config.max_output_size == 16384

    def test_init_worker_stores_output_capture_options(self) -> None:
        """capture_output and max_output_size reach the config."""
        init_worker(
            MagicMock(),
            transport_url="memory://",
            capture_output=True,
            max_output_size=4096,
        )

        config = get_config()
        assert config is not None
        assert config.capture_output is True
        assert config.max_output_size == 4096

    @pytest.mark.parametrize("size", [0, -1])
    def test_init_worker_rejects_non_positive_max_output_size(self, size: int) -> None:
        """A zero or negative output cap is a configuration error."""
        with pytest.raises(ConfigurationError, match="max_output_size"):
            init_worker(
                MagicMock(),
                transport_url="memory://",
                capture_output=True,
                max_output_size=size,
            )

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
                form_auth_config: object = None,
                node_alias_from_arguments: str | None = None,
            ) -> None:
                captured["broker_url"] = broker_url
                captured["transport_url"] = transport_url
                del (
                    embedded_consumer,
                    serve_ui,
                    prefix,
                    ttl,
                    max_nodes,
                    auth_dependency,
                    form_auth_config,
                    node_alias_from_arguments,
                )

            def init_app(self, _app: FastAPI, *, prefix: str | None = None) -> None:
                del prefix

        monkeypatch.setenv(
            "STEMTRACE_BROKER_URL", "amqp://guest:guest@localhost:5672//"
        )
        monkeypatch.setenv("STEMTRACE_TRANSPORT_URL", "redis://localhost:6379/0")
        monkeypatch.setattr(stemtrace, "StemtraceExtension", _FakeExtension)

        app = FastAPI()
        init_app(app, broker_url=None, embedded_consumer=False, serve_ui=False)

        assert captured["broker_url"] == "amqp://guest:guest@localhost:5672//"
        assert captured["transport_url"] == "redis://localhost:6379/0"

    def test_init_worker_with_node_alias_warns_deprecated(self) -> None:
        """node_alias_from_arguments on init_worker warns: it has no worker effect."""
        app = MagicMock()

        with pytest.warns(DeprecationWarning, match="no effect on workers") as record:
            init_worker(
                app,
                transport_url="memory://",
                node_alias_from_arguments="operator_type",
            )

        assert len(record) == 1
        assert record[0].filename == __file__
        assert "STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS" in str(record[0].message)

    def test_init_worker_with_node_alias_logs_warning_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A log warning is emitted once, since Celery workers hide the warning."""
        app = MagicMock()
        caplog.set_level(logging.WARNING, logger=_INIT_LOGGER)

        with pytest.warns(DeprecationWarning) as record:  # noqa: PT030, PT031
            for _ in range(2):
                init_worker(
                    app,
                    transport_url="memory://",
                    node_alias_from_arguments="operator_type",
                )

        assert len(record) == 2
        logged = [r for r in caplog.records if r.name == _INIT_LOGGER]
        assert len(logged) == 1
        assert logged[0].levelno == logging.WARNING
        message = logged[0].getMessage()
        assert "no effect on workers" in message
        assert "STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS" in message
        assert "--node-alias-from-arguments" in message

    def test_init_worker_without_node_alias_does_not_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """init_worker() emits no warning or log when the option is omitted."""
        app = MagicMock()
        caplog.set_level(logging.WARNING, logger=_INIT_LOGGER)

        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            init_worker(app, transport_url="memory://")

        assert [r for r in caplog.records if r.name == _INIT_LOGGER] == []
        config = get_config()
        assert config is not None
        assert config.node_alias_from_arguments is None


class TestInitAppNodeAlias:
    """Tests for node_alias_from_arguments resolution in init_app()."""

    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
        """Swap StemtraceExtension for a fake that records its kwargs."""
        recorded: dict[str, object] = {}

        class _FakeExtension:
            def __init__(self, broker_url: str, **kwargs: object) -> None:
                recorded["broker_url"] = broker_url
                recorded.update(kwargs)

            def init_app(self, _app: FastAPI, *, prefix: str | None = None) -> None:
                del prefix

        monkeypatch.setattr(stemtrace, "StemtraceExtension", _FakeExtension)
        monkeypatch.delenv("STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS", raising=False)
        return recorded

    def test_init_app_without_kwarg_uses_env_var(
        self, captured: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS is used when the kwarg is omitted."""
        monkeypatch.setenv("STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS", "operator_type")

        init_app(FastAPI(), broker_url="memory://")

        assert captured["node_alias_from_arguments"] == "operator_type"

    def test_init_app_with_kwarg_overrides_env_var(
        self, captured: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit node_alias_from_arguments wins over the env var."""
        monkeypatch.setenv("STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS", "operator_type")

        init_app(FastAPI(), broker_url="memory://", node_alias_from_arguments="0")

        assert captured["node_alias_from_arguments"] == "0"

    def test_init_app_with_empty_env_var_passes_none(
        self, captured: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty env var counts as unset."""
        monkeypatch.setenv("STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS", "")

        init_app(FastAPI(), broker_url="memory://")

        assert captured["node_alias_from_arguments"] is None

    def test_init_app_with_padded_env_var_strips_whitespace(
        self, captured: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Surrounding whitespace in the env var is stripped."""
        monkeypatch.setenv("STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS", " operator_type ")

        init_app(FastAPI(), broker_url="memory://")

        assert captured["node_alias_from_arguments"] == "operator_type"

    def test_init_app_with_blank_env_var_passes_none(
        self, captured: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A whitespace-only env var counts as unset."""
        monkeypatch.setenv("STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS", "  ")

        init_app(FastAPI(), broker_url="memory://")

        assert captured["node_alias_from_arguments"] is None

    def test_init_app_with_padded_kwarg_strips_whitespace(
        self, captured: dict[str, object]
    ) -> None:
        """Surrounding whitespace in an explicit kwarg is stripped."""
        init_app(
            FastAPI(),
            broker_url="memory://",
            node_alias_from_arguments=" operator_type ",
        )

        assert captured["node_alias_from_arguments"] == "operator_type"

    @pytest.mark.parametrize("value", ["", "  "])
    def test_init_app_with_blank_kwarg_passes_none(
        self, captured: dict[str, object], value: str
    ) -> None:
        """An empty or whitespace-only kwarg counts as unset."""
        init_app(FastAPI(), broker_url="memory://", node_alias_from_arguments=value)

        assert captured["node_alias_from_arguments"] is None

    def test_init_app_with_blank_kwarg_falls_back_to_env_var(
        self, captured: dict[str, object], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A blank kwarg is treated like an omitted one, so the env var applies."""
        monkeypatch.setenv("STEMTRACE_NODE_ALIAS_FROM_ARGUMENTS", "operator_type")

        init_app(FastAPI(), broker_url="memory://", node_alias_from_arguments="")

        assert captured["node_alias_from_arguments"] == "operator_type"

    def test_init_app_without_kwarg_or_env_var_passes_none(
        self, captured: dict[str, object]
    ) -> None:
        """Without kwarg or env var the extension gets None (task names are shown)."""
        init_app(FastAPI(), broker_url="memory://")

        assert captured["node_alias_from_arguments"] is None


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
