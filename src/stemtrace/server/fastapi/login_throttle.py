"""In-memory throttling of failed sign-in attempts for the built-in login.

Failed attempts are counted per client IP, and per username for browsers
without a *device cookie*. After too many failures a key is locked out for an
exponentially growing time, and the login endpoint answers 429 with
``Retry-After`` (without checking the credentials) until the lock expires.

The device cookie follows OWASP's "device cookie" lockout pattern: a
successful sign-in sets a long-lived signed cookie naming that browser. A
browser presenting a valid one for the submitted username is counted under its
own device key instead of the username, so failures by strangers who know the
username can't lock its owner out of a browser that has signed in before.

State is per process: with several workers or replicas, each one counts on
its own, and a restart clears it.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from stemtrace.server.fastapi.form_auth import sign_session, verify_session

if TYPE_CHECKING:
    from collections.abc import Callable

    from stemtrace.server.fastapi.form_auth import FormAuthConfig

#: Failures allowed per client IP before it is locked out.
LOGIN_IP_MAX_FAILURES: Final[int] = 5

#: Failures allowed per username, from all IPs together, for browsers without
#: a valid device cookie for it.
LOGIN_USERNAME_MAX_FAILURES: Final[int] = 20

#: Failures allowed per device cookie before that device is locked out.
LOGIN_DEVICE_MAX_FAILURES: Final[int] = 5

#: First lockout; each further failure while over the limit doubles it.
LOGIN_LOCKOUT_BASE_S: Final[float] = 30.0

#: Longest lockout.
LOGIN_LOCKOUT_MAX_S: Final[float] = 900.0

#: A key is forgotten this long after its last failure. Longer than the longest
#: lockout, so a key at the cap stays at one attempt per lockout instead of
#: getting a fresh set of free attempts when its lock ends.
LOGIN_FAILURE_MEMORY_S: Final[float] = 3600.0

#: Unlocked keys tracked per kind (IP, username, device). To make room for a
#: new key, a forgotten key is dropped first, else the least recently failed
#: key with the fewest failures, but only one at no more than half its limit
#: (see ``_FailureTracker``). If none qualifies, new keys are refused (429)
#: until one is forgotten (fail closed), except for browsers with a valid
#: device cookie.
LOGIN_THROTTLE_MAX_KEYS: Final[int] = 10_000

#: Locked keys held per kind. Locks are never dropped before they expire; when
#: this many are held, keys that aren't locked yet are refused (429) until one
#: expires (fail closed), except for browsers with a valid device cookie.
LOGIN_THROTTLE_MAX_LOCKED: Final[int] = 10_000

#: How long a device cookie stays valid after a successful sign-in.
DEVICE_COOKIE_TTL_SECONDS: Final[int] = 90 * 86400

# Doublings beyond which the lockout is at its cap anyway; bounds 2.0**n so a
# key with thousands of failures can't overflow the float.
_MAX_DOUBLINGS: Final[int] = math.ceil(
    math.log2(LOGIN_LOCKOUT_MAX_S / LOGIN_LOCKOUT_BASE_S)
)

_DEVICE_KEY_LABEL: Final[bytes] = b"stemtrace-login-device-cookie"


def device_cookie_name(config: FormAuthConfig) -> str:
    """Name of the device cookie that goes with this config's session cookie."""
    return f"{config.cookie_name}_device"


def _device_secret(config: FormAuthConfig) -> str:
    """Signing key for device cookies, derived from the session secret.

    A separate key keeps the two cookies apart: a device cookie, which lives
    for months, must never verify as a session cookie, and vice versa.
    """
    return hmac.new(
        config.secret.encode("utf-8"), _DEVICE_KEY_LABEL, hashlib.sha256
    ).hexdigest()


def _username_digest(config: FormAuthConfig, username: str) -> str:
    """Keyed digest of a username, so the cookie doesn't carry it in clear."""
    return hmac.new(
        _device_secret(config).encode("utf-8"),
        username.encode("utf-8", errors="replace"),
        hashlib.sha256,
    ).hexdigest()


def create_device_cookie_value(config: FormAuthConfig) -> str:
    """Create a signed device cookie for this config's user and a new device id.

    The payload is only signed, not encrypted, so it holds a keyed digest of
    the username rather than the username itself.
    """
    payload: dict[str, object] = {
        "d": secrets.token_urlsafe(16),
        "u": _username_digest(config, config.username),
        "exp": int(time.time()) + DEVICE_COOKIE_TTL_SECONDS,
    }
    return sign_session(payload, _device_secret(config))


def device_id_from_cookie(
    cookie_value: str | None, config: FormAuthConfig, username: str
) -> str | None:
    """Return the device id of a valid device cookie issued for ``username``.

    Args:
        cookie_value: Device cookie from the request, if any.
        config: Form auth configuration (its secret signs device cookies).
        username: Username submitted with this sign-in attempt.

    Returns:
        The device id, or None if the cookie is missing, forged, expired or
        issued for another username.
    """
    payload = verify_session(cookie_value, secret=_device_secret(config))
    if payload is None:
        return None
    issued_for = payload.get("u")
    device_id = payload.get("d")
    if not isinstance(issued_for, str) or not isinstance(device_id, str):
        return None
    if not device_id or not secrets.compare_digest(
        issued_for.encode("utf-8"),
        _username_digest(config, username).encode("utf-8"),
    ):
        return None
    return device_id


@dataclass(frozen=True, slots=True)
class LoginAttempt:
    """Who is trying to sign in, as far as throttling is concerned.

    Attributes:
        client_ip: Client address from the ASGI scope, or None if the server
            reported none (e.g. a Unix socket).
        username: Submitted username, valid or not.
        device_id: Device id from a valid device cookie for ``username``
            (see :func:`device_id_from_cookie`), or None.
    """

    client_ip: str | None
    username: str
    device_id: str | None


@dataclass(slots=True)
class _Failures:
    """Failure history of one key."""

    count: int
    last_failure: float
    locked_until: float


class _FailureTracker:
    """Failure counts for one kind of key, with bounded memory.

    Locked keys live in their own table (at most ``max_locked``) and are never
    dropped before their lock ends, so a flood of new keys can't clear a lock.
    While it is full, ``retry_after(..., fail_closed=True)`` refuses every key
    that isn't locked yet until the earliest lock ends.

    Unlocked keys are bucketed by failure count, each bucket in least recently
    failed order. A new key may push out a forgotten key, or else the least
    recently failed key of the lowest bucket, but only from buckets at no more
    than half the limit. So junk keys can't push out a key that is close to
    being locked (a flood would otherwise reset its count and allow unlimited
    guesses), and that holds for every key alike, so eviction reveals nothing
    about which username is real. When the unlocked table is full of keys at
    more than half the limit, new keys can't be tracked, and
    ``retry_after(..., fail_closed=True)`` refuses them until one is
    forgotten.

    Total memory stays at most ``max_keys + max_locked`` keys: new keys only
    enter while fewer than ``max_keys`` are unlocked, and keys whose lock ends
    move back from the locked table (so they never lose their count).
    """

    def __init__(
        self,
        *,
        max_failures: int,
        max_keys: int,
        max_locked: int,
        clock: Callable[[], float],
    ) -> None:
        """Initialize an empty tracker."""
        self._max_failures = max_failures
        self._evictable_max = max_failures // 2
        self._max_keys = max_keys
        self._max_locked = max_locked
        self._clock = clock
        # Bucket b holds unlocked keys with min(count, max_failures) == b.
        self._buckets: list[OrderedDict[str, _Failures]] = [
            OrderedDict() for _ in range(max_failures + 1)
        ]
        self._bucket_of: dict[str, int] = {}
        self._locked: dict[str, _Failures] = {}
        self._next_unlock = math.inf

    def __len__(self) -> int:
        """Number of keys currently held (locked or not)."""
        return len(self._bucket_of) + len(self._locked)

    def retry_after(self, key: str, *, fail_closed: bool) -> float:
        """Seconds until ``key`` may try again (0.0 when allowed).

        Args:
            key: The IP, username digest or device id.
            fail_closed: Refuse keys whose failures couldn't be tracked or
                lead to a lock right now (tables full), instead of allowing
                them untracked.
        """
        now = self._clock()
        self._release_expired_locks(now)
        entry = self._locked.get(key)
        if entry is not None:
            return entry.locked_until - now
        if fail_closed and len(self._locked) >= self._max_locked:
            return self._next_unlock - now
        entry = self._get_unlocked(key)
        if entry is not None and self._is_forgotten(entry, now):
            self._pop_unlocked(key)
            entry = None
        if entry is None and fail_closed and self._room_victim(now) is False:
            return self._soonest_forgotten(now) - now
        return 0.0

    def record_failure(self, key: str) -> None:
        """Count a failure for ``key`` and lock it once over the limit.

        A new key that can't be tracked (tables full) isn't recorded; callers
        that fail closed never get here for one. When the locked table is
        full, a key over the limit keeps counting but isn't locked.
        """
        now = self._clock()
        self._release_expired_locks(now)
        entry = self._locked.pop(key, None) or self._pop_unlocked(key)
        if entry is not None and self._is_forgotten(entry, now):
            entry = None
        if entry is None:
            if not self._make_room(now):
                return
            entry = _Failures(count=0, last_failure=now, locked_until=0.0)
        entry.count += 1
        entry.last_failure = now
        over = entry.count - self._max_failures
        if over >= 0 and len(self._locked) < self._max_locked:
            lockout = LOGIN_LOCKOUT_BASE_S * 2.0 ** min(over, _MAX_DOUBLINGS)
            entry.locked_until = now + min(LOGIN_LOCKOUT_MAX_S, lockout)
            self._locked[key] = entry
            self._next_unlock = min(self._next_unlock, entry.locked_until)
        else:
            self._put_unlocked(key, entry)

    def reset(self, key: str) -> None:
        """Forget ``key`` (after a successful sign-in)."""
        self._pop_unlocked(key)
        if self._locked.pop(key, None) is not None:
            self._recompute_next_unlock()

    @staticmethod
    def _is_forgotten(entry: _Failures, now: float) -> bool:
        return now >= entry.last_failure + LOGIN_FAILURE_MEMORY_S

    def _get_unlocked(self, key: str) -> _Failures | None:
        bucket = self._bucket_of.get(key)
        return None if bucket is None else self._buckets[bucket][key]

    def _pop_unlocked(self, key: str) -> _Failures | None:
        bucket = self._bucket_of.pop(key, None)
        return None if bucket is None else self._buckets[bucket].pop(key)

    def _put_unlocked(self, key: str, entry: _Failures) -> None:
        bucket = min(entry.count, self._max_failures)
        self._buckets[bucket][key] = entry
        self._bucket_of[key] = bucket

    def _room_victim(self, now: float) -> str | bool:
        """Which key must go to fit a new one.

        Returns:
            True if there is room already, else the key to drop (a forgotten
            one first, else the least recently failed key of the lowest
            bucket up to half the limit), or False if none may go.
        """
        if len(self._bucket_of) < self._max_keys:
            return True
        for bucket in self._buckets:
            if bucket:
                key, oldest = next(iter(bucket.items()))
                if self._is_forgotten(oldest, now):
                    return key
        for bucket in self._buckets[: self._evictable_max + 1]:
            if bucket:
                return next(iter(bucket))
        return False

    def _make_room(self, now: float) -> bool:
        """Free a slot for a new unlocked key; False if none may be freed."""
        victim = self._room_victim(now)
        if isinstance(victim, str):
            self._pop_unlocked(victim)
            return True
        return victim

    def _soonest_forgotten(self, now: float) -> float:
        """When the next unlocked key will be forgotten (frees a slot)."""
        fronts = (next(iter(b.values())) for b in self._buckets if b)
        return min(
            (e.last_failure + LOGIN_FAILURE_MEMORY_S for e in fronts), default=now
        )

    def _recompute_next_unlock(self) -> None:
        self._next_unlock = min(
            (e.locked_until for e in self._locked.values()), default=math.inf
        )

    def _release_expired_locks(self, now: float) -> None:
        """Move keys whose lock ended back among the unlocked (keeping counts).

        Scans the locked table only once the earliest lock has ended, so a
        flood costs one scan per expiry, not one per request.
        """
        if now < self._next_unlock:
            return
        released = [k for k, e in self._locked.items() if e.locked_until <= now]
        for key in released:
            self._put_unlocked(key, self._locked.pop(key))
        self._recompute_next_unlock()


class LoginThrottle:
    """Throttle failed sign-ins per client IP, per username and per device.

    - Every failed attempt with a client address counts against that IP
      (IPv6 addresses by their /64 network, which one client usually holds).
    - An attempt with a valid device cookie for the submitted username counts
      against that device; any other attempt counts against the username.
    - A valid device cookie also skips the IP check (its failures still
      count), so strangers sharing an IP with the owner (e.g. every request
      arriving from a proxy whose address isn't trusted) can't lock out a
      browser that signed in before.
    - Requests without a client address (e.g. uvicorn ``--uds``) skip the IP
      limit; only the username/device limits apply to them.

    Execution model: the login handler is ``async`` and calls this without
    awaiting in between, so on one event loop each check-then-update is
    atomic. A lock still guards every method, so calls from other threads
    (e.g. a sync test client or a threaded server) are safe; it is never held
    across I/O. State is in memory and per process (see the module docstring).

    The client IP is whatever the ASGI server put in ``scope["client"]``.
    Forwarded headers are never read here: behind a reverse proxy, configure
    the server to trust that proxy (uvicorn ``--forwarded-allow-ips`` /
    ``FORWARDED_ALLOW_IPS``), or every user shares the proxy's address.

    Usernames are stored as SHA-256 digests, so an attacker can't grow memory
    with long usernames.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = LOGIN_THROTTLE_MAX_KEYS,
        max_locked: int = LOGIN_THROTTLE_MAX_LOCKED,
    ) -> None:
        """Initialize with an injectable monotonic clock and per-kind caps."""
        self._lock = threading.Lock()
        self._by_ip, self._by_username, self._by_device = (
            _FailureTracker(
                max_failures=max_failures,
                max_keys=max_keys,
                max_locked=max_locked,
                clock=clock,
            )
            for max_failures in (
                LOGIN_IP_MAX_FAILURES,
                LOGIN_USERNAME_MAX_FAILURES,
                LOGIN_DEVICE_MAX_FAILURES,
            )
        )

    @property
    def tracked_keys(self) -> int:
        """Total number of IP, username and device keys held in memory."""
        with self._lock:
            return len(self._by_ip) + len(self._by_username) + len(self._by_device)

    def retry_after(self, attempt: LoginAttempt) -> int:
        """Whole seconds until this attempt may be checked (0: allowed).

        Args:
            attempt: The sign-in attempt.

        Returns:
            0 if no applicable key is locked (or refused while the tables are
            full), else the longest remaining wait, rounded up (the
            ``Retry-After`` value).
        """
        with self._lock:
            if attempt.device_id is not None:
                wait = self._by_device.retry_after(attempt.device_id, fail_closed=False)
            else:
                waits = [
                    self._by_username.retry_after(
                        _username_key(attempt.username), fail_closed=True
                    )
                ]
                if attempt.client_ip:
                    waits.append(
                        self._by_ip.retry_after(
                            _ip_key(attempt.client_ip), fail_closed=True
                        )
                    )
                wait = max(waits)
        return math.ceil(wait)

    def record_failure(self, attempt: LoginAttempt) -> None:
        """Count a failed sign-in against the IP and the device or username."""
        with self._lock:
            if attempt.client_ip:
                self._by_ip.record_failure(_ip_key(attempt.client_ip))
            if attempt.device_id is not None:
                self._by_device.record_failure(attempt.device_id)
            else:
                self._by_username.record_failure(_username_key(attempt.username))

    def record_success(self, attempt: LoginAttempt) -> None:
        """Clear the failure history of the IP, the username and the device."""
        with self._lock:
            if attempt.client_ip:
                self._by_ip.reset(_ip_key(attempt.client_ip))
            if attempt.device_id is not None:
                self._by_device.reset(attempt.device_id)
            self._by_username.reset(_username_key(attempt.username))


def _username_key(username: str) -> str:
    """Fixed-size key for a submitted username."""
    return hashlib.sha256(username.encode("utf-8", errors="replace")).hexdigest()


def _ip_key(client_ip: str) -> str:
    """Throttling key for a client address: IPv6 by /64, IPv4 as is.

    An IPv6 client typically controls a whole /64, so keying by address would
    give it 2**64 fresh IP limits. IPv4-mapped IPv6 addresses count as their
    IPv4 address. Anything that isn't an IP address is used verbatim.
    """
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return client_ip
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)
