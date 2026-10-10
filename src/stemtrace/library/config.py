"""Configuration for stemtrace library."""

from pydantic import BaseModel, ConfigDict, Field


class StemtraceConfig(BaseModel):
    """Frozen configuration for stemtrace initialization.

    Attributes:
        transport_url: Broker URL for event transport.
        prefix: Redis key prefix for stemtrace data.
        ttl: Time-to-live for stored events in seconds.
        capture_args: Whether to capture task args/kwargs.
        capture_result: Whether to capture task return values.
        max_data_size: Maximum size in bytes for serialized data.
        scrub_sensitive_data: Whether to scrub sensitive keys.
        additional_sensitive_keys: Extra keys to treat as sensitive.
        safe_keys: Keys to never scrub (overrides sensitive).
        node_alias_from_arguments: Deprecated, unused by workers. Stored for
            backward compatibility only; node aliases are resolved by the
            server (``init_app()`` / ``stemtrace server
            --node-alias-from-arguments``).
        capture_output: Whether to capture what each task attempt prints and
            logs and attach it to the attempt's terminal event.
        max_output_size: Maximum UTF-8 bytes of output kept per attempt (the
            end of the output is kept).
    """

    model_config = ConfigDict(frozen=True)

    transport_url: str
    prefix: str = "stemtrace"
    ttl: int = 86400

    # Data capture options (all enabled by default)
    capture_args: bool = True
    capture_result: bool = True
    max_data_size: int = 10240  # 10KB

    # Scrubbing options (enabled by default)
    scrub_sensitive_data: bool = True
    additional_sensitive_keys: frozenset[str] = Field(default_factory=frozenset)
    safe_keys: frozenset[str] = Field(default_factory=frozenset)

    # Deprecated: unused on the worker side (resolved by the server)
    node_alias_from_arguments: str | None = None

    # Output capture options (disabled by default)
    capture_output: bool = False
    max_output_size: int = Field(default=16384, gt=0)  # 16 KB


_config: StemtraceConfig | None = None


def get_config() -> StemtraceConfig | None:
    """Get the active configuration, or None if not initialized."""
    return _config


def set_config(config: StemtraceConfig) -> None:
    """Set the active configuration."""
    global _config
    _config = config


def _reset_config() -> None:
    """Reset configuration. For testing only."""
    global _config
    _config = None
