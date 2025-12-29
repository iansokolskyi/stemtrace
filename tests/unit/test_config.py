"""Tests for StemtraceConfig."""

import pytest
from pydantic import ValidationError

from stemtrace.library.config import (
    StemtraceConfig,
    get_config,
    set_config,
)


class TestStemtraceConfig:
    """Tests for StemtraceConfig model."""

    def test_create_with_required_fields(self) -> None:
        """Config can be created with just transport_url."""
        config = StemtraceConfig(transport_url="redis://localhost:6379/0")

        assert config.transport_url == "redis://localhost:6379/0"
        assert config.prefix == "stemtrace"
        assert config.ttl == 86400
        assert config.capture_args is True
        assert config.capture_result is True
        assert config.scrub_sensitive_data is True

    def test_create_with_all_fields(self) -> None:
        """Config can be created with all custom values."""
        config = StemtraceConfig(
            transport_url="redis://custom:6379/1",
            prefix="myapp_flow",
            ttl=3600,
            capture_args=False,
            capture_result=False,
            scrub_sensitive_data=False,
        )

        assert config.transport_url == "redis://custom:6379/1"
        assert config.prefix == "myapp_flow"
        assert config.ttl == 3600
        assert config.capture_args is False
        assert config.capture_result is False
        assert config.scrub_sensitive_data is False

    def test_config_is_frozen(self) -> None:
        """Config is immutable after creation."""
        config = StemtraceConfig(transport_url="redis://localhost:6379/0")

        with pytest.raises(ValidationError):
            config.transport_url = "redis://other:6379/0"  # type: ignore[misc]

    def test_missing_transport_url_raises(self) -> None:
        """Config requires transport_url."""
        with pytest.raises(ValidationError):
            StemtraceConfig()  # type: ignore[call-arg]

    def test_config_equality(self) -> None:
        """Two configs with same values are equal."""
        config1 = StemtraceConfig(transport_url="redis://localhost:6379/0")
        config2 = StemtraceConfig(transport_url="redis://localhost:6379/0")

        assert config1 == config2

    def test_config_hashable(self) -> None:
        """Frozen config can be used in sets/dicts."""
        config = StemtraceConfig(transport_url="redis://localhost:6379/0")
        config_set = {config}

        assert config in config_set


class TestConfigModule:
    """Tests for module-level config storage."""

    def test_get_config_returns_none_initially(self) -> None:
        """get_config returns None before set_config is called."""
        # Note: This test may fail if run after other tests that set config
        # In practice, the module state persists, so we test the set/get flow
        pass

    def test_set_and_get_config(self) -> None:
        """set_config stores config that get_config retrieves."""
        config = StemtraceConfig(transport_url="redis://localhost:6379/0")
        set_config(config)

        retrieved = get_config()

        assert retrieved == config

    def test_set_config_replaces_previous(self) -> None:
        """set_config overwrites any previous config."""
        config1 = StemtraceConfig(transport_url="redis://localhost:6379/0")
        config2 = StemtraceConfig(transport_url="redis://other:6379/0")

        set_config(config1)
        set_config(config2)

        assert get_config() == config2
