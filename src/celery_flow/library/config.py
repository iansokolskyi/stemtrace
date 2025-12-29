"""Configuration for celery-flow library."""

from pydantic import BaseModel, ConfigDict, Field


class CeleryFlowConfig(BaseModel):
    """Frozen configuration for celery-flow initialization.

    Attributes:
        transport_url: Broker URL for event transport.
        prefix: Redis key prefix for celery-flow data.
        ttl: Time-to-live for stored events in seconds.
        capture_args: Whether to capture task args/kwargs.
        capture_result: Whether to capture task return values.
        max_data_size: Maximum size in bytes for serialized data.
        scrub_sensitive_data: Whether to scrub sensitive keys.
        additional_sensitive_keys: Extra keys to treat as sensitive.
        safe_keys: Keys to never scrub (overrides sensitive).
    """

    model_config = ConfigDict(frozen=True)

    transport_url: str
    prefix: str = "celery_flow"
    ttl: int = 86400

    # Data capture options (all enabled by default)
    capture_args: bool = True
    capture_result: bool = True
    max_data_size: int = 10240  # 10KB

    # Scrubbing options (enabled by default)
    scrub_sensitive_data: bool = True
    additional_sensitive_keys: frozenset[str] = Field(default_factory=frozenset)
    safe_keys: frozenset[str] = Field(default_factory=frozenset)


_config: CeleryFlowConfig | None = None


def get_config() -> CeleryFlowConfig | None:
    """Get the active configuration, or None if not initialized."""
    return _config


def set_config(config: CeleryFlowConfig) -> None:
    """Set the active configuration."""
    global _config
    _config = config


def _reset_config() -> None:
    """Reset configuration. For testing only."""
    global _config
    _config = None
