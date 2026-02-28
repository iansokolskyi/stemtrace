"""Redis transport using Redis Streams."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from typing_extensions import Self

from stemtrace.core.events import TaskEvent, WorkerEvent

if TYPE_CHECKING:
    from collections.abc import Iterator

    from redis import Redis

logger = logging.getLogger(__name__)

# Type alias for all events that can be consumed from the stream
StreamEvent = TaskEvent | WorkerEvent

# Mapping from CERT_* constants (used in URLs) to lowercase values expected by redis-py
_SSL_CERT_REQS_MAP = {
    "CERT_NONE": "none",
    "CERT_OPTIONAL": "optional",
    "CERT_REQUIRED": "required",
}


def _normalize_redis_ssl_params(url: str) -> str:
    """Normalize ssl_cert_reqs in a Redis URL from CERT_* to lowercase.

    redis-py expects lowercase values (none/optional/required) but URLs
    commonly use the Python ssl module constants (CERT_NONE/CERT_REQUIRED).
    """
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    parsed = urlparse(url)
    if not parsed.query:
        return url
    params = parse_qs(parsed.query)
    if "ssl_cert_reqs" not in params:
        return url
    raw = params["ssl_cert_reqs"][0]
    normalized = _SSL_CERT_REQS_MAP.get(raw, raw)
    params["ssl_cert_reqs"] = [normalized]
    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


class RedisTransport:
    """Redis Streams-based event transport (XADD/XREAD)."""

    def __init__(self, client: Redis[Any], prefix: str, ttl: int) -> None:
        """Initialize Redis transport with client and stream configuration."""
        self._client = client
        self._ttl = ttl
        self._stream_key = f"{prefix}:events"
        self._maxlen = max(ttl, 10000)

    @property
    def client(self) -> Redis[Any]:
        """The Redis client."""
        return self._client

    @property
    def stream_key(self) -> str:
        """The stream key."""
        return self._stream_key

    @property
    def ttl(self) -> int:
        """TTL in seconds."""
        return self._ttl

    def publish(self, event: StreamEvent) -> None:
        """Publish an event to the Redis stream.

        Fire-and-forget: logs errors, never raises.

        Args:
            event: Event to publish (TaskEvent or WorkerEvent).
        """
        try:
            self._client.xadd(
                self._stream_key,
                {"data": event.model_dump_json()},
                maxlen=self._maxlen,
                approximate=True,
            )
        except Exception:
            event_id = self._event_identifier(event)
            logger.warning(
                "Failed to publish event %s to Redis", event_id, exc_info=True
            )

    @staticmethod
    def _event_identifier(event: StreamEvent) -> str:
        """Create a stable identifier for logging."""
        if isinstance(event, TaskEvent):
            return event.task_id
        return f"{event.hostname}:{event.pid}:{event.event_type}"

    def consume(self, last_id: str = "0") -> Iterator[StreamEvent]:
        """Blocking iterator that yields events as they arrive.

        Detects event type from JSON and yields appropriate model
        (TaskEvent or WorkerEvent).
        """
        current_id = last_id
        while True:
            results = self._client.xread(
                {self._stream_key: current_id},
                block=5000,
                count=100,
            )
            if not results:
                continue

            for _stream_name, messages in results:
                for message_id, fields in messages:
                    current_id = (
                        message_id.decode()
                        if isinstance(message_id, bytes)
                        else message_id
                    )
                    data = fields.get(b"data") or fields.get("data")
                    if data:
                        data_str = data.decode() if isinstance(data, bytes) else data
                        try:
                            yield self._parse_event(data_str)
                        except (json.JSONDecodeError, ValidationError, ValueError):
                            logger.warning(
                                "Failed to parse event from Redis stream %s at id %s",
                                self._stream_key,
                                current_id,
                                exc_info=True,
                            )
                            continue

    def _parse_event(self, data_str: str) -> StreamEvent:
        """Parse JSON into appropriate event type."""
        # Peek at JSON to determine event type
        parsed = json.loads(data_str)
        if "event_type" in parsed:
            # WorkerEvent has event_type field
            return WorkerEvent.model_validate(parsed)
        if "task_id" in parsed:
            # TaskEvent has task_id field
            return TaskEvent.model_validate(parsed)
        raise ValueError("Unknown event payload: missing event_type and task_id")

    @classmethod
    def from_url(cls, url: str, prefix: str = "stemtrace", ttl: int = 86400) -> Self:
        """Create transport from Redis URL."""
        from redis import Redis as RedisClient

        url = _normalize_redis_ssl_params(url)
        return cls(
            client=RedisClient.from_url(url, decode_responses=False),
            prefix=prefix,
            ttl=ttl,
        )
