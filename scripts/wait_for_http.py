"""Wait for a local HTTP endpoint to respond with a 2xx status.

Used by the E2E Makefile targets and CI workflow to wait for the Docker Compose
stack. Only loopback hosts and the E2E Compose server service are accepted, so
the script cannot be pointed at arbitrary internal or external network targets.

A bare 2xx is not always "ready": the stemtrace health endpoint answers 200 while
its broker consumer is still connecting. On RabbitMQ events published before the
consumer binds its queue are lost (the fanout exchange drops unbound messages); the
Redis consumer reads the stream from the start, so there the check only covers the
``consumer_connected: null`` window before its first read. Two optional checks run
against the JSON response body:

* ``--expect-json KEY=VALUE`` (repeatable) requires a top-level field, e.g.
  ``--expect-json consumer_connected=true`` on ``/stemtrace/api/health``. VALUE is
  parsed as JSON (``true``, ``3``, ``null``) and otherwise taken as a string.
* ``--expect-task NAME=STATE[:COUNT]`` (repeatable) requires the stemtrace task list
  (``/stemtrace/api/tasks``) to hold at least COUNT (default 1) tasks called exactly
  NAME in state STATE.
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
PROGRESS_INTERVAL_S = 10.0
# Mirrors stemtrace.core.events.TaskState (a unit test keeps them in sync). Not
# imported: importing the stemtrace package pulls in celery and fastapi, which
# makes this stdlib-only poller take seconds to start.
TASK_STATES = (
    "PENDING",
    "RECEIVED",
    "STARTED",
    "SUCCESS",
    "FAILURE",
    "REVOKED",
    "REJECTED",
    "RETRY",
    "IGNORED",
)
_MISSING = object()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so polling never leaves the validated host.

    A 3xx response then surfaces as an ``HTTPError`` and counts as unhealthy.
    """

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        """Return None so urllib raises HTTPError instead of following."""


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


class TaskExpectation(typing.NamedTuple):
    """At least ``min_count`` tasks named ``name`` must be in state ``state``."""

    name: str
    state: str
    min_count: int = 1

    def __str__(self) -> str:
        """Render as ``NAME=STATE`` for progress and timeout messages."""
        return f"{self.name}={self.state}"


class JsonExpectation(typing.NamedTuple):
    """The response's top-level field ``key`` must equal ``value``."""

    key: str
    value: object
    text: str


def _parse_task_expectation(spec: str) -> TaskExpectation:
    """Parse ``NAME=STATE[:COUNT]`` or raise ``ArgumentTypeError``."""
    name, _, rest = spec.partition("=")
    state, _, count_text = rest.partition(":")
    min_count = 1
    if count_text:
        try:
            min_count = int(count_text)
        except ValueError:
            min_count = 0
    if not name or not state or min_count < 1:
        raise argparse.ArgumentTypeError(
            f"{spec!r} is not NAME=STATE[:COUNT] with COUNT >= 1"
        )
    if state not in TASK_STATES:
        raise argparse.ArgumentTypeError(
            f"{spec!r} has unknown state {state!r}; expected one of "
            f"{', '.join(TASK_STATES)}"
        )
    return TaskExpectation(name=name, state=state, min_count=min_count)


def _parse_json_expectation(spec: str) -> JsonExpectation:
    """Parse ``KEY=VALUE`` (VALUE as JSON, else a string) or raise."""
    key, _, text = spec.partition("=")
    if not key or not text:
        raise argparse.ArgumentTypeError(f"{spec!r} is not KEY=VALUE")
    try:
        value: object = json.loads(text)
    except ValueError:
        value = text
    return JsonExpectation(key=key, value=value, text=text)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="health URL on a loopback host or E2E service")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument(
        "--expect-json",
        action="append",
        default=[],
        type=_parse_json_expectation,
        metavar="KEY=VALUE",
        help="also require the JSON response to have top-level KEY equal to VALUE",
    )
    parser.add_argument(
        "--expect-task",
        action="append",
        default=[],
        type=_parse_task_expectation,
        metavar="NAME=STATE[:COUNT]",
        help="also require the response to list COUNT tasks named NAME in STATE",
    )
    return parser.parse_args()


def _unmet_json_expectations(
    payload: dict[str, object], expectations: list[JsonExpectation]
) -> str | None:
    """Return which expected fields differ in ``payload``, or None if all match."""
    unmet = []
    for expected in expectations:
        got = payload.get(expected.key, _MISSING)
        # type() too, so that JSON true does not satisfy an expected 1.
        if got == expected.value and type(got) is type(expected.value):
            continue
        shown = "missing" if got is _MISSING else f"got {json.dumps(got)}"
        unmet.append(f"{expected.key}={expected.text} ({shown})")
    return f"waiting for fields: {', '.join(unmet)}" if unmet else None


def _unmet_task_expectations(
    payload: dict[str, object], expectations: list[TaskExpectation]
) -> str | None:
    """Return why the task list in ``payload`` falls short, or None if it holds.

    Task names are compared exactly: the API's ``name`` filter is a substring
    match, so ``e2e.add`` must not be satisfied by ``e2e.add_many``.
    """
    try:
        tasks = typing.cast("list[dict[str, str]]", payload["tasks"])
        seen = collections.Counter((task["name"], task["state"]) for task in tasks)
    except (KeyError, TypeError) as exc:
        return f"unexpected task list response ({type(exc).__name__}: {exc})"

    unmet = [
        f"{expected} ({seen[(expected.name, expected.state)]}/{expected.min_count})"
        for expected in expectations
        if seen[(expected.name, expected.state)] < expected.min_count
    ]
    if not unmet:
        return None
    # str() keys: a null name or state must not break the sort of a failure message.
    by_name_state = sorted(seen.items(), key=lambda item: tuple(map(str, item[0])))
    found = ", ".join(f"{name}={state} x{n}" for (name, state), n in by_name_state)
    return f"waiting for tasks: {', '.join(unmet)}; seen: {found or 'none'}"


def _unmet_expectations(
    body: bytes,
    json_expectations: list[JsonExpectation],
    task_expectations: list[TaskExpectation],
) -> str | None:
    """Return why the response body is not ready yet, or None if it is."""
    try:
        payload = json.loads(body)
    except ValueError as exc:  # invalid or truncated JSON, or not UTF-8
        return f"response is not valid JSON ({exc})"
    if not isinstance(payload, dict):
        return f"response is not a JSON object (got {type(payload).__name__})"

    reasons = [
        reason
        for reason in (
            _unmet_json_expectations(payload, json_expectations),
            _unmet_task_expectations(payload, task_expectations)
            if task_expectations
            else None,
        )
        if reason
    ]
    return "; ".join(reasons) or None


def _poll(
    url: str,
    json_expectations: list[JsonExpectation],
    task_expectations: list[TaskExpectation],
) -> str | None:
    """Poll ``url`` once; return None when ready, else why it is not."""
    try:
        request = urllib.request.Request(url, method="GET")
        with _OPENER.open(request, timeout=REQUEST_TIMEOUT_S) as response:
            if not 200 <= response.status < 300:
                return f"HTTP {response.status}"
            if not json_expectations and not task_expectations:
                return None
            return _unmet_expectations(
                response.read(MAX_BODY_BYTES), json_expectations, task_expectations
            )
    except urllib.error.HTTPError as exc:
        exc.close()
        return f"HTTP {exc.code}"
    except (OSError, http.client.HTTPException) as exc:
        # URLError, timeouts and connection resets while the stack starts, and
        # IncompleteRead when the body is cut short.
        return str(exc)


def main() -> None:
    """Wait for a local HTTP endpoint to become ready or exit non-zero."""
    args = _parse_args()
    url: str = args.url
    parsed = urlparse(url)
    if parsed.scheme in ALLOWED_SCHEMES and parsed.hostname in ALLOWED_HOSTS:
        start = time.monotonic()
        deadline = start + args.timeout
        last_progress = start
        now = start
        last_error: str | None = None

        while now < deadline:
            last_error = _poll(url, args.expect_json, args.expect_task)
            if last_error is None:
                return
            if now - last_progress >= PROGRESS_INTERVAL_S:
                last_progress = now
                print(
                    f"still waiting after {now - start:.0f}s of {args.timeout:g}s: "
                    f"{last_error}",
                    flush=True,
                )
            time.sleep(args.interval)
            now = time.monotonic()

        raise SystemExit(f"Service not healthy within {args.timeout}s: {last_error}")

    allowed = ", ".join(sorted(ALLOWED_HOSTS))
    raise SystemExit(
        f"Refusing to poll {url!r}: only http(s) URLs on {allowed} are allowed"
    )


if __name__ == "__main__":
    main()
