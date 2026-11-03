"""Worker registry refresh that runs without a request."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    import threading

INSPECT_PROBE_STOP_TIMEOUT_SECONDS = 2.0


def _clock() -> float:
    return time.monotonic()


def _wait(stop: threading.Event, seconds: float) -> bool:
    return stop.wait(seconds)


class InspectProbe:
    """Stub."""

    def __init__(
        self,
        min_interval_s: float,
        *,
        clock: Callable[[], float] | None = None,
        wait: Callable[[threading.Event, float], bool] | None = None,
    ) -> None:
        """Stub."""

    def bind(self, refresh: Callable[[], bool]) -> None:
        """Stub."""

    def trigger(self) -> None:
        """Stub."""

    def start(self) -> None:
        """Stub."""

    def stop(self, timeout: float | None = None) -> None:
        """Stub."""
