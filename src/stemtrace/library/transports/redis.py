"""Redis transport using Redis Streams."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from typing_extensions import Self

from stemtrace.core.events import TaskEvent

if TYPE_CHECKING:
    from collections.abc import Iterator

    from redis import Redis

logger = logging.getLogger(__name__)


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

    def publish(self, event: TaskEvent) -> None:
        """Publish event. Fire-and-forget: logs errors, never raises."""
        try:
            self._client.xadd(
                self._stream_key,
                {"data": event.model_dump_json()},
                maxlen=self._maxlen,
                approximate=True,
            )
        except Exception:
            logger.warning(
                "Failed to publish event %s to Redis", event.task_id, exc_info=True
            )

    def consume(self, last_id: str = "0") -> Iterator[TaskEvent]:
        """Blocking iterator that yields events as they arrive."""
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
                        yield TaskEvent.model_validate_json(data_str)

    @classmethod
    def from_url(cls, url: str, prefix: str = "stemtrace", ttl: int = 86400) -> Self:
        """Create transport from Redis URL."""
        from redis import Redis as RedisClient

        return cls(
            client=RedisClient.from_url(url, decode_responses=False),
            prefix=prefix,
            ttl=ttl,
        )
