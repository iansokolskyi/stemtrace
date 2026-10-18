"""In-memory throttling of failed sign-in attempts for the built-in login.

Failures are counted per client IP, and per username for browsers without a
*device cookie* (OWASP's device-cookie lockout pattern): a browser that signed in
before counts under its own device key, so strangers who know the username can't
lock it out. A locked key gets 429 with ``Retry-After``, without a credential
check, for an exponentially growing time. Counts live in fixed-size hashed tables,
so memory is constant; they are per process and a restart clears them. The limits
users see are in docs/security.md.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import secrets
import threading
import time
from array import array
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
#: getting a fresh set of free attempts when its lock ends. This bounds
#: guessing per username: a greedy attacker gets the 20 free failures, then
#: one per lockout (about 4 an hour at the 15-minute cap); a patient one waits
#: out this hour after each burst of 20, so about 20 an hour is the sustained
#: maximum either way.
LOGIN_FAILURE_MEMORY_S: Final[float] = 3600.0

#: Buckets per kind of key (IP, username, device). Failures are counted in
#: fixed tables of this size, indexed by a keyed hash, so memory is constant
#: and nothing ever evicts or resets another key's count (see _BucketTable).
LOGIN_THROTTLE_BUCKETS: Final[int] = 65_536

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
        username: Submitted username, valid or not. None for attempts that
            have no username (an API key): only the IP limit applies to them,
            since a shared counter would let anyone lock the key out for
            every client.
        device_id: Device id from a valid device cookie for ``username``
            (see :func:`device_id_from_cookie`), or None.
    """

    client_ip: str | None
    username: str | None
    device_id: str | None


class _BucketTable:
    """Failure counts for one kind of key in a fixed table of hashed buckets.

    Each key maps to two buckets (keyed BLAKE2b, random per-process key) and
    waits the *minimum* of their locks. Other keys only add to a bucket, so
    collisions can lock a key early but never late, and nothing is evicted (a
    count-min sketch). Only ``reset`` after a successful sign-in lowers counts.
    """

    def __init__(
        self,
        *,
        max_failures: int,
        buckets: int,
        hash_key: bytes,
        label: bytes,
        clock: Callable[[], float],
    ) -> None:
        """Initialize a table of ``buckets`` empty buckets (constant memory)."""
        self._max_failures = max_failures
        self._size = buckets
        self._hash_key = hash_key
        self._label = label
        self._clock = clock
        self._counts = array("Q", bytes(8 * buckets))
        self._last_failure = array("d", bytes(8 * buckets))
        self._locked_until = array("d", bytes(8 * buckets))

    @property
    def memory_bytes(self) -> int:
        """Bytes held by the bucket arrays (constant for the table's life)."""
        arrays = (self._counts, self._last_failure, self._locked_until)
        return sum(a.itemsize * len(a) for a in arrays)

    def retry_after(self, key: str) -> float:
        """Seconds until ``key`` may try again (0.0 when allowed)."""
        now = self._clock()
        return max(0.0, min(self._wait(i, now) for i in self._slots(key)))

    def failures_left(self, key: str) -> int:
        """Failures ``key`` may still have before its next lockout (0 once locked)."""
        now = self._clock()
        counts = (
            0 if self._is_forgotten(i, now) else int(self._counts[i])
            for i in self._slots(key)
        )
        return max(0, self._max_failures - min(counts))

    def record_failure(self, key: str) -> None:
        """Count a failure for ``key`` in both of its buckets."""
        now = self._clock()
        for i in set(self._slots(key)):
            if self._is_forgotten(i, now):
                self._counts[i] = 0
                self._locked_until[i] = 0.0
            self._counts[i] += 1
            self._last_failure[i] = now
            over = self._counts[i] - self._max_failures
            if over >= 0:
                lockout = LOGIN_LOCKOUT_BASE_S * 2.0 ** min(over, _MAX_DOUBLINGS)
                self._locked_until[i] = now + min(LOGIN_LOCKOUT_MAX_S, lockout)

    def reset(self, key: str) -> None:
        """Clear ``key``'s buckets after a successful sign-in."""
        for i in self._slots(key):
            self._counts[i] = 0
            self._last_failure[i] = 0.0
            self._locked_until[i] = 0.0

    def _slots(self, key: str) -> tuple[int, int]:
        digest = hashlib.blake2b(
            key.encode("utf-8", errors="replace"),
            digest_size=16,
            key=self._hash_key,
            person=self._label,
        ).digest()
        return (
            int.from_bytes(digest[:8], "big") % self._size,
            int.from_bytes(digest[8:], "big") % self._size,
        )

    def _is_forgotten(self, i: int, now: float) -> bool:
        return now >= self._last_failure[i] + LOGIN_FAILURE_MEMORY_S

    def _wait(self, i: int, now: float) -> float:
        if self._counts[i] == 0 or self._is_forgotten(i, now):
            return 0.0
        return self._locked_until[i] - now


class LoginThrottle:
    """Throttle failed sign-ins per client IP, per username and per device.

    A valid device cookie for the submitted username counts against the device
    and skips the IP check; other attempts count against the username and the
    IP (IPv6 by /64; none without a client address). The IP is
    ``scope["client"]``: forwarded headers are never read here. Thread-safe.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        buckets: int = LOGIN_THROTTLE_BUCKETS,
        hash_key: bytes | None = None,
    ) -> None:
        """Initialize empty tables.

        Args:
            clock: Monotonic clock (injectable for tests).
            buckets: Buckets per kind of key.
            hash_key: Key for the bucket hash; a random per-process key if
                None. Only tests pass one, for reproducible collisions.
        """
        # The async login handler is atomic per event loop anyway; the lock is
        # for other threads (sync test client, threaded server), never held
        # across I/O.
        self._lock = threading.Lock()
        # Attempts in flight per IP key; only IPs with one in flight have an entry.
        self._ip_in_flight: dict[str, int] = {}
        key = hash_key if hash_key is not None else secrets.token_bytes(32)
        self._by_ip, self._by_username, self._by_device = (
            _BucketTable(
                max_failures=max_failures,
                buckets=buckets,
                hash_key=key,
                label=label,
                clock=clock,
            )
            for max_failures, label in (
                (LOGIN_IP_MAX_FAILURES, b"stemtrace-ip"),
                (LOGIN_USERNAME_MAX_FAILURES, b"stemtrace-user"),
                (LOGIN_DEVICE_MAX_FAILURES, b"stemtrace-device"),
            )
        )

    @property
    def memory_bytes(self) -> int:
        """Bytes held by the bucket tables (constant)."""
        tables = (self._by_ip, self._by_username, self._by_device)
        return sum(t.memory_bytes for t in tables)

    def retry_after(self, attempt: LoginAttempt) -> int:
        """Whole seconds until this attempt may be checked (0: allowed).

        Args:
            attempt: The sign-in attempt.

        Returns:
            0 if no applicable key is locked, else the longest remaining
            lockout, rounded up (the ``Retry-After`` value).
        """
        with self._lock:
            if attempt.device_id is not None:
                wait = self._by_device.retry_after(attempt.device_id)
            else:
                wait = 0.0
                if attempt.username is not None:
                    wait = self._by_username.retry_after(attempt.username)
                if attempt.client_ip:
                    ip_wait = self._by_ip.retry_after(_ip_key(attempt.client_ip))
                    wait = max(wait, ip_wait)
        return math.ceil(wait)

    def ip_has_capacity(self, client_ip: str) -> bool:
        """Whether ``client_ip`` may start an attempt whose failure isn't counted yet.

        False while its attempts in flight already cover the failures it has
        left before a lockout, so a burst can't try more keys than the lockout
        allows. Always True with none in flight.
        """
        key = _ip_key(client_ip)
        with self._lock:
            held = self._ip_in_flight.get(key, 0)
            return held == 0 or held < self._by_ip.failures_left(key)

    def hold_ip_attempt(self, client_ip: str) -> None:
        """Count an attempt in flight for ``client_ip`` until it is released."""
        key = _ip_key(client_ip)
        with self._lock:
            self._ip_in_flight[key] = self._ip_in_flight.get(key, 0) + 1

    def release_ip_attempt(self, client_ip: str) -> None:
        """End an attempt counted by :meth:`hold_ip_attempt`."""
        key = _ip_key(client_ip)
        with self._lock:
            left = self._ip_in_flight.get(key, 0) - 1
            if left > 0:
                self._ip_in_flight[key] = left
            else:
                self._ip_in_flight.pop(key, None)

    def record_failure(self, attempt: LoginAttempt) -> None:
        """Count a failed sign-in against the IP and the device or username."""
        with self._lock:
            if attempt.client_ip:
                self._by_ip.record_failure(_ip_key(attempt.client_ip))
            if attempt.device_id is not None:
                self._by_device.record_failure(attempt.device_id)
            elif attempt.username is not None:
                self._by_username.record_failure(attempt.username)

    def record_success(self, attempt: LoginAttempt) -> None:
        """Clear the failure history of the IP, the username and the device."""
        with self._lock:
            if attempt.client_ip:
                self._by_ip.reset(_ip_key(attempt.client_ip))
            if attempt.device_id is not None:
                self._by_device.reset(attempt.device_id)
            if attempt.username is not None:
                self._by_username.reset(attempt.username)


def _parse_client_address(
    client_ip: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse a client address, dropping a ``:port`` / ``[v6]:port`` suffix.

    Some proxies (e.g. Azure Application Gateway) append the client's source
    port to X-Forwarded-For, which uvicorn passes on as the client host.
    """
    candidates = [client_ip]
    if client_ip.startswith("["):
        host, _, rest = client_ip[1:].partition("]")
        if not rest or (rest.startswith(":") and rest[1:].isdigit()):
            candidates = [host]
    elif client_ip.count(":") == 1:
        host, _, port = client_ip.partition(":")
        if port.isdigit():
            candidates.append(host)
    for candidate in candidates:
        try:
            return ipaddress.ip_address(candidate)
        except ValueError:
            continue
    return None


def _ip_key(client_ip: str) -> str:
    """Throttling key for a client address: IPv6 by /64, IPv4 as is.

    An IPv6 client typically controls a whole /64, so keying by address would
    give it 2**64 fresh IP limits. IPv4-mapped IPv6 addresses count as their
    IPv4 address, and a source port is ignored. Anything that isn't an IP
    address is used verbatim.
    """
    address = _parse_client_address(client_ip)
    if address is None:
        return client_ip
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)
