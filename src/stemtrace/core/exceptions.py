"""Domain exceptions for stemtrace."""


class StemtraceError(Exception):
    """Base exception for stemtrace."""


class ConfigurationError(StemtraceError):
    """Invalid or missing configuration."""


class TransportError(StemtraceError):
    """Transport connection, publish, or consume failure."""


class UnsupportedBrokerError(ConfigurationError):
    """Broker URL scheme not supported."""

    def __init__(self, scheme: str) -> None:
        """Initialize with the unsupported broker scheme."""
        self.scheme = scheme
        super().__init__(
            f"Unsupported broker scheme: '{scheme}'. "
            f"Supported: redis, rediss, amqp, amqps, pyamqp, memory"
        )
