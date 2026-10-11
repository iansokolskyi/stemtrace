"""Wait for a local HTTP endpoint to respond with a 2xx status.

Used by the E2E Makefile targets and CI workflow to wait for the Docker Compose
stack. Only loopback hosts and the E2E Compose server service are accepted, so
the script cannot be pointed at arbitrary internal or external network targets.

With ``--expect-task NAME=STATE[:COUNT]`` (repeatable) the endpoint must also be
the stemtrace task list (``/stemtrace/api/tasks``) and the wait only ends once
it holds at least COUNT (default 1) tasks called exactly NAME in state STATE.
"""

from __future__ import annotations

import argparse
import collections
import http.client
import json
import time
import typing
import urllib.error
import urllib.request
from urllib.parse import urlparse

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "server"})
# A seeded task list is a few KiB; cap the body so a misbehaving endpoint
# cannot make the wait read without bound.
MAX_BODY_BYTES = 16 * 1024 * 1024
REQUEST_TIMEOUT_S = 2.0


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so polling never leaves the validated host.

    A 3xx response then surfaces as an ``HTTPError`` and counts as unhealthy.
    """

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        """Return None so urllib raises HTTPError instead of following."""


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


class TaskExpectation(typing.NamedTuple):
    """At least ``count`` tasks named ``name`` must be in state ``state``."""

    name: str
    state: str
    count: int = 1

    def __str__(self) -> str:
        """Render as ``NAME=STATE`` for progress and timeout messages."""
        return f"{self.name}={self.state}"


def _parse_expectation(spec: str) -> TaskExpectation:
    """Parse ``NAME=STATE[:COUNT]`` or raise ``ArgumentTypeError``."""
    name, _, rest = spec.partition("=")
    state, _, count_text = rest.partition(":")
    count = 1
    if count_text:
        try:
            count = int(count_text)
        except ValueError:
            count = 0
    if not name or not state or count < 1:
        raise argparse.ArgumentTypeError(
            f"{spec!r} is not NAME=STATE[:COUNT] with COUNT >= 1"
        )
    return TaskExpectation(name=name, state=state, count=count)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="health URL on a loopback host or E2E service")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument(
        "--expect-task",
        action="append",
        default=[],
        type=_parse_expectation,
        metavar="NAME=STATE[:COUNT]",
        help="also require the response to list COUNT tasks named NAME in STATE",
    )
    return parser.parse_args()


def _unmet_task_expectations(
    body: bytes, expectations: list[TaskExpectation]
) -> str | None:
    """Return why ``body`` (a task list response) falls short, or None if it holds.

    Task names are compared exactly: the API's ``name`` filter is a substring
    match, so ``e2e.add`` must not be satisfied by ``e2e.add_many``.
    """
    try:
        payload = json.loads(body)
        tasks = payload["tasks"]
        seen = collections.Counter((task["name"], task["state"]) for task in tasks)
    except (ValueError, KeyError, TypeError) as exc:
        return f"unexpected task list response ({type(exc).__name__}: {exc})"

    unmet = [
        f"{expected} ({seen[(expected.name, expected.state)]}/{expected.count})"
        for expected in expectations
        if seen[(expected.name, expected.state)] < expected.count
    ]
    if not unmet:
        return None
    found = ", ".join(
        f"{name}={state} x{n}" for (name, state), n in sorted(seen.items())
    )
    return f"waiting for tasks: {', '.join(unmet)}; seen: {found or 'none'}"


def _poll(url: str, expectations: list[TaskExpectation]) -> str | None:
    """Poll ``url`` once; return None when healthy, else why it is not."""
    try:
        request = urllib.request.Request(url, method="GET")
        with _OPENER.open(request, timeout=REQUEST_TIMEOUT_S) as response:
            if not 200 <= response.status < 300:
                return f"HTTP {response.status}"
            if not expectations:
                return None
            return _unmet_task_expectations(response.read(MAX_BODY_BYTES), expectations)
    except urllib.error.HTTPError as exc:
        exc.close()
        return f"HTTP {exc.code}"
    except (OSError, http.client.HTTPException) as exc:
        # URLError, timeouts and connection resets while the stack starts
        return str(exc)


def main() -> None:
    """Wait for a local HTTP endpoint to become healthy (2xx) or exit non-zero."""
    args = _parse_args()
    url: str = args.url
    parsed = urlparse(url)
    if parsed.scheme in ALLOWED_SCHEMES and parsed.hostname in ALLOWED_HOSTS:
        deadline = time.monotonic() + args.timeout
        last_error: str | None = None

        while time.monotonic() < deadline:
            last_error = _poll(url, args.expect_task)
            if last_error is None:
                return
            time.sleep(args.interval)

        raise SystemExit(f"Service not healthy within {args.timeout}s: {last_error}")

    allowed = ", ".join(sorted(ALLOWED_HOSTS))
    raise SystemExit(
        f"Refusing to poll {url!r}: only http(s) URLs on {allowed} are allowed"
    )


if __name__ == "__main__":
    main()
