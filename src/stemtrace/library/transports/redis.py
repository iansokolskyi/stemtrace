"""Redis transport using Redis Streams."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from typing_extensions import Self

from stemtrace.core.events import TaskEvent, WorkerEvent

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from redis import Redis

logger = logging.getLogger(__name__)

# Type alias for all events that can be consumed from the stream
StreamEvent = TaskEvent | WorkerEvent

# How long a single XREAD blocks waiting for new entries.
XREAD_BLOCK_MS = 5000

# Socket timeout applied by from_url() when the URL doesn't set one. It must be
# longer than the XREAD block window, otherwise an idle stream times out the
# socket (redis-py 8 defaults socket_timeout to 5s, the same as the window).
DEFAULT_SOCKET_TIMEOUT = XREAD_BLOCK_MS / 1000 + 5.0

# Consecutive XREAD read timeouts tolerated silently before warning.
READ_TIMEOUT_WARN_AFTER = 3

# Minimum seconds between repeated read-timeout warnings.
READ_TIMEOUT_WARN_INTERVAL = 60.0

# Reconnect backoff after connection failures: doubles per failure, resets on success.
RECONNECT_INITIAL_BACKOFF = 1.0
RECONNECT_MAX_BACKOFF = 30.0

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


def _is_connect_timeout(exc: Exception) -> bool:
    """Whether a redis-py TimeoutError came from connecting rather than reading."""
    return str(exc).startswith("Timeout connecting")


def _xread_block_ms(client: Redis[Any]) -> int:
    """Pick an XREAD block window shorter than the client's socket timeout.

    Clients created by from_url() get DEFAULT_SOCKET_TIMEOUT, so this is
    XREAD_BLOCK_MS. A caller-supplied client or URL may set a shorter
    socket_timeout; blocking longer than that would time out every idle read.
    """
    pool = getattr(client, "connection_pool", None)
    kwargs = getattr(pool, "connection_kwargs", None)
    timeout = kwargs.get("socket_timeout") if isinstance(kwargs, dict) else None
    if isinstance(timeout, int | float) and 0 < timeout * 1000 <= XREAD_BLOCK_MS:
        return max(100, int(timeout * 1000) // 2)
    return XREAD_BLOCK_MS


class RedisTransport:
    """Redis Streams-based event transport (XADD/XREAD)."""

    def __init__(
        self,
        client: Redis[Any],
        prefix: str,
        ttl: int,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize Redis transport with client and stream configuration.

        Args:
            client: Redis client used for XADD/XREAD.
            prefix: Key prefix; the stream key is ``<prefix>:events``.
            ttl: Event retention hint, also used to size the stream.
            sleep: Sleep function for reconnect backoff (injectable for tests).
            monotonic: Clock used to rate-limit warnings (injectable for tests).
        """
        self._client = client
        self._ttl = ttl
        self._stream_key = f"{prefix}:events"
        self._maxlen = max(ttl, 10000)
        self._sleep = sleep
        self._monotonic = monotonic
        self._block_ms = _xread_block_ms(client)
        self._last_id = "0"
        self._connected: bool | None = None

    @property
    def connected(self) -> bool | None:
        """Whether the last XREAD reached Redis.

        None until the first read completes, True after a successful read,
        False while reads are failing (connection errors or repeated timeouts).
        """
        return self._connected

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

    def consume(self, last_id: str | None = None) -> Iterator[StreamEvent]:
        """Blocking iterator that yields events as they arrive.

        Detects event type from JSON and yields appropriate model
        (TaskEvent or WorkerEvent).

        Connection errors and connect timeouts never end the iterator: the
        transport logs a warning, backs off exponentially (capped at
        RECONNECT_MAX_BACKOFF seconds, reset after a successful read) and
        resumes from the last stream id it saw. An occasional read timeout is
        retried immediately; repeated read timeouts are logged as warnings.

        Args:
            last_id: Stream id to read after. Defaults to the last id this
                transport consumed ("0", the start of the stream, initially).
        """
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        if last_id is not None:
            self._last_id = last_id
        failures = 0
        read_timeouts = 0
        backoff = RECONNECT_INITIAL_BACKOFF
        last_timeout_warning: float | None = None

        while True:
            try:
                results = self._client.xread(
                    {self._stream_key: self._last_id},
                    block=self._block_ms,
                    count=100,
                )
            except RedisTimeoutError as exc:
                if _is_connect_timeout(exc):
                    failures += 1
                    read_timeouts = 0
                    backoff = self._back_off(failures, backoff, exc)
                    continue
                read_timeouts += 1
                if read_timeouts < READ_TIMEOUT_WARN_AFTER:
                    logger.debug("XREAD on %s timed out; retrying", self._stream_key)
                    continue
                self._connected = False
                now = self._monotonic()
                if (
                    last_timeout_warning is None
                    or now - last_timeout_warning >= READ_TIMEOUT_WARN_INTERVAL
                ):
                    last_timeout_warning = now
                    logger.warning(
                        "XREAD on %s timed out %d times in a row; check the "
                        "network and the socket_timeout setting",
                        self._stream_key,
                        read_timeouts,
                    )
                continue
            except RedisConnectionError as exc:
                failures += 1
                read_timeouts = 0
                backoff = self._back_off(failures, backoff, exc)
                continue

            if failures:
                logger.info(
                    "Redis consumer on %s reconnected after %d failure(s)",
                    self._stream_key,
                    failures,
                )
            failures = 0
            read_timeouts = 0
            backoff = RECONNECT_INITIAL_BACKOFF
            last_timeout_warning = None
            self._connected = True

            if not results:
                continue

            for _stream_name, messages in results:
                for message_id, fields in messages:
                    self._last_id = (
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
                                self._last_id,
                                exc_info=True,
                            )
                            continue

    def _back_off(self, failures: int, backoff: float, exc: Exception) -> float:
        """Log a consume failure, sleep, and return the next backoff delay."""
        self._connected = False
        if failures == 1:
            logger.warning(
                "Redis consumer on %s cannot reach Redis; retrying in %.1fs",
                self._stream_key,
                backoff,
                exc_info=True,
            )
        else:
            logger.warning(
                "Redis consumer on %s still failing (attempt %d): %s; "
                "retrying in %.1fs",
                self._stream_key,
                failures,
                exc,
                backoff,
            )
        self._sleep(backoff)
        return min(backoff * 2, RECONNECT_MAX_BACKOFF)

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
        """Create transport from Redis URL.

        Sets ``socket_timeout`` to DEFAULT_SOCKET_TIMEOUT so a blocking XREAD
        on an idle stream returns normally instead of timing out the socket.
        A ``socket_timeout`` in the URL query string takes precedence.
        """
        from redis import Redis as RedisClient

        url = _normalize_redis_ssl_params(url)
        return cls(
            client=RedisClient.from_url(
                url,
                decode_responses=False,
                socket_timeout=DEFAULT_SOCKET_TIMEOUT,
            ),
            prefix=prefix,
            ttl=ttl,
        )
