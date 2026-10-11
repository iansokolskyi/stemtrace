"""In-memory throttling of failed sign-in attempts for the built-in login.

Failed attempts are counted per client IP and per submitted username. After
too many failures a key is locked out for an exponentially growing time, and
the login endpoint answers 429 with ``Retry-After`` (without checking the
credentials) until the lock expires. A successful sign-in clears both keys.

The state is per process: with several workers or replicas, each one counts on
its own, so the effective limit is multiplied by the number of processes.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable

#: Failures allowed per client IP before it is locked out.
LOGIN_IP_MAX_FAILURES: Final[int] = 5

#: Failures allowed per username before it is locked out, from all IPs together.
#: Higher than the per-IP limit because anyone can trigger it for the real
#: username and lock its owner out; it bounds guessing spread over many IPs.
LOGIN_USERNAME_MAX_FAILURES: Final[int] = 20

#: First lockout; each further failure while over the limit doubles it.
LOGIN_LOCKOUT_BASE_S: Final[float] = 30.0

#: Longest lockout.
LOGIN_LOCKOUT_MAX_S: Final[float] = 900.0

#: A key is forgotten this long after its last failure. Longer than the longest
#: lockout, so a key at the cap stays at one attempt per lockout instead of
#: getting a fresh set of free attempts when its lock ends.
LOGIN_FAILURE_MEMORY_S: Final[float] = 3600.0

#: Keys tracked per kind (IP, username). Beyond it, the least recently failed
#: key is dropped, so memory stays bounded under a flood of distinct keys.
LOGIN_THROTTLE_MAX_KEYS: Final[int] = 10_000


@dataclass(slots=True)
class _Failures:
    """Failure history of one key."""

    count: int
    last_failure: float
    locked_until: float


class _FailureTracker:
    """Failure counts for one kind of key, bounded in size.

    Entries are kept in order of their last failure (oldest first). Since a
    lock never outlasts ``LOGIN_FAILURE_MEMORY_S`` after the last failure, an
    entry's expiry is monotonic in that order, so expired entries are always
    at the front.
    """

    def __init__(
        self,
        *,
        max_failures: int,
        max_keys: int,
        clock: Callable[[], float],
    ) -> None:
        """Initialize an empty tracker."""
        self._max_failures = max_failures
        self._max_keys = max_keys
        self._clock = clock
        self._entries: OrderedDict[str, _Failures] = OrderedDict()

    def __len__(self) -> int:
        """Number of keys currently tracked."""
        return len(self._entries)

    def retry_after(self, key: str) -> float:
        """Seconds until ``key`` may try again (0.0 when not locked)."""
        entry = self._entries.get(key)
        if entry is None:
            return 0.0
        now = self._clock()
        if self._is_expired(entry, now):
            del self._entries[key]
            return 0.0
        return max(0.0, entry.locked_until - now)

    def record_failure(self, key: str) -> None:
        """Count a failure for ``key`` and lock it once over the limit."""
        now = self._clock()
        entry = self._entries.pop(key, None)
        if entry is None or self._is_expired(entry, now):
            entry = _Failures(count=0, last_failure=now, locked_until=0.0)
            self._make_room(now)
        entry.count += 1
        entry.last_failure = now
        over = entry.count - self._max_failures
        if over >= 0:
            lockout = min(LOGIN_LOCKOUT_MAX_S, LOGIN_LOCKOUT_BASE_S * 2.0**over)
            entry.locked_until = now + lockout
        self._entries[key] = entry

    def reset(self, key: str) -> None:
        """Forget ``key`` (after a successful sign-in)."""
        self._entries.pop(key, None)

    @staticmethod
    def _is_expired(entry: _Failures, now: float) -> bool:
        return now >= entry.last_failure + LOGIN_FAILURE_MEMORY_S

    def _make_room(self, now: float) -> None:
        """Drop expired entries, then the oldest ones, to fit one more key."""
        while self._entries:
            oldest = next(iter(self._entries.values()))
            if not self._is_expired(oldest, now):
                break
            self._entries.popitem(last=False)
        while len(self._entries) >= self._max_keys:
            self._entries.popitem(last=False)


class LoginThrottle:
    """Throttle failed sign-ins per client IP and per username.

    Execution model: the login handler is ``async`` and calls this without
    awaiting in between, so on one event loop each check-then-update is
    atomic. A lock still guards every method, so calls from other threads
    (e.g. a sync test client or a threaded server) are safe; it is never held
    across I/O. State is in memory and per process (see the module docstring).

    The client IP is whatever the ASGI server put in ``scope["client"]``.
    Forwarded headers are never read here: behind a reverse proxy, configure
    the server to trust it (uvicorn ``--forwarded-allow-ips`` /
    ``FORWARDED_ALLOW_IPS``), or every user shares the proxy's address.

    Usernames are stored as SHA-256 digests, so an attacker can't grow memory
    with long usernames and the configured username isn't kept in clear twice.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = LOGIN_THROTTLE_MAX_KEYS,
    ) -> None:
        """Initialize with an injectable monotonic clock and a per-kind key cap."""
        self._lock = threading.Lock()
        self._by_ip = _FailureTracker(
            max_failures=LOGIN_IP_MAX_FAILURES, max_keys=max_keys, clock=clock
        )
        self._by_username = _FailureTracker(
            max_failures=LOGIN_USERNAME_MAX_FAILURES, max_keys=max_keys, clock=clock
        )

    @property
    def tracked_keys(self) -> int:
        """Total number of IP and username keys currently held in memory."""
        with self._lock:
            return len(self._by_ip) + len(self._by_username)

    def retry_after(self, client_ip: str, username: str) -> int:
        """Whole seconds until this IP and username may try again (0: allowed).

        Args:
            client_ip: Client address from the ASGI scope.
            username: Submitted username, valid or not.

        Returns:
            0 if neither key is locked, else the longer remaining lockout,
            rounded up (the ``Retry-After`` value).
        """
        user_key = _username_key(username)
        with self._lock:
            wait = max(
                self._by_ip.retry_after(client_ip),
                self._by_username.retry_after(user_key),
            )
        return math.ceil(wait)

    def record_failure(self, client_ip: str, username: str) -> None:
        """Count a failed sign-in against both the IP and the username."""
        user_key = _username_key(username)
        with self._lock:
            self._by_ip.record_failure(client_ip)
            self._by_username.record_failure(user_key)

    def record_success(self, client_ip: str, username: str) -> None:
        """Clear the failure history of the IP and the username."""
        user_key = _username_key(username)
        with self._lock:
            self._by_ip.reset(client_ip)
            self._by_username.reset(user_key)


def _username_key(username: str) -> str:
    """Fixed-size key for a submitted username."""
    return hashlib.sha256(username.encode("utf-8", errors="replace")).hexdigest()
