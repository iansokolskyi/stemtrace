"""Queue and route configuration shared by the event-fields worker and its sender.

Three routes: the default queue, a queue named in ``task_routes`` and a queue
bound to a topic exchange that ``task_routes`` sends to with a routing key.
Both sides must route alike, as a real producer and worker share their
configuration. No side effects on import (the test process imports it).
"""

from typing import Any

from kombu import Exchange, Queue

TOPIC_ROUTING_KEY = "media.video"


class Names:
    """Queue, exchange and stream names for one test run."""

    def __init__(self, suffix: str) -> None:
        """Make the names unique to ``suffix``."""
        self.default_queue = f"ef-default-{suffix}"
        self.routed_queue = f"ef-routed-{suffix}"
        self.topic_queue = f"ef-topic-{suffix}"
        self.topic_exchange = f"ef-topic-{suffix}"
        self.events_prefix = f"ef_test_{suffix}"


def routing_config(names: Names) -> dict[str, Any]:
    """Celery settings for the three routes."""
    return {
        "task_serializer": "json",
        "accept_content": ["json"],
        "task_default_queue": names.default_queue,
        "task_queues": [
            Queue(names.default_queue),
            Queue(names.routed_queue),
            Queue(
                names.topic_queue,
                Exchange(names.topic_exchange, type="topic"),
                "media.#",
            ),
        ],
        "task_routes": {
            "ef.routed": {"queue": names.routed_queue},
            "ef.topic": {
                "queue": names.topic_queue,
                "routing_key": TOPIC_ROUTING_KEY,
            },
        },
    }
