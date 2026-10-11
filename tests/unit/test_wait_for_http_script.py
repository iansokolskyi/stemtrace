"""Tests for the scripts/wait_for_http.py E2E helper."""

from __future__ import annotations

import http.client
import importlib.util
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from stemtrace.core.events import TaskState

if TYPE_CHECKING:
    import urllib.request

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "wait_for_http.py"
_spec = importlib.util.spec_from_file_location("wait_for_http", SCRIPT)
assert _spec is not None
assert _spec.loader is not None
wait_for_http = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wait_for_http)


def _run(monkeypatch: pytest.MonkeyPatch, url: str) -> str:
    monkeypatch.setattr("sys.argv", ["wait_for_http.py", url, "--timeout", "0"])
    with pytest.raises(SystemExit) as exc_info:
        wait_for_http.main()
    return str(exc_info.value.code)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/health",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://internal.example:8000/stemtrace/api/health",
        "https://localhost.example.com/health",
        "https://localhost@example.com/health",
        "file:///etc/passwd",
        "ftp://localhost/health",
        "localhost:8000/health",
    ],
)
def test_main_with_disallowed_url_exits_with_refusal(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    message = _run(monkeypatch, url)

    assert message.startswith(f"Refusing to poll {url!r}")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/stemtrace/api/health",
        "http://127.0.0.1:8000/stemtrace/api/health",
        "http://[::1]:8000/stemtrace/api/health",
        "https://localhost/health",
        "https://server:8000/stemtrace/api/health",
    ],
)
def test_main_with_allowed_url_reaches_polling_loop(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    message = _run(monkeypatch, url)

    assert message == "Service not healthy within 0.0s: None"


class _FakeResponse:
    status = 200

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_main_when_connection_resets_retries_until_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[Exception] = [
        ConnectionResetError(104, "Connection reset by peer"),
        http.client.RemoteDisconnected("Remote end closed connection"),
    ]
    calls: list[str] = []

    def fake_open(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        calls.append(request.full_url)
        if errors:
            raise errors.pop(0)
        return _FakeResponse()

    monkeypatch.setattr(wait_for_http._OPENER, "open", fake_open)
    monkeypatch.setattr(wait_for_http.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "sys.argv",
        ["wait_for_http.py", "http://localhost:8000/health", "--timeout", "30"],
    )

    wait_for_http.main()

    assert calls == ["http://localhost:8000/health"] * 3


class _JsonResponse(_FakeResponse):
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]


def _tasks(*pairs: tuple[str, str]) -> dict[str, object]:
    return {
        "tasks": [
            {"task_id": f"id-{i}", "name": name, "state": state}
            for i, (name, state) in enumerate(pairs)
        ],
        "total": len(pairs),
    }


def _serve(monkeypatch: pytest.MonkeyPatch, payloads: list[object]) -> list[str]:
    """Answer successive polls with ``payloads`` (the last one repeats)."""
    calls: list[str] = []

    def fake_open(request: urllib.request.Request, timeout: float) -> _JsonResponse:
        calls.append(request.full_url)
        index = min(len(calls) - 1, len(payloads) - 1)
        return _JsonResponse(payloads[index])

    monkeypatch.setattr(wait_for_http._OPENER, "open", fake_open)
    monkeypatch.setattr(wait_for_http.time, "sleep", lambda _seconds: None)
    return calls


def _run_expecting(
    monkeypatch: pytest.MonkeyPatch,
    *expectations: str,
    timeout: str = "30",
    json_fields: tuple[str, ...] = (),
) -> str | None:
    argv = ["wait_for_http.py", "http://localhost:8000/stemtrace/api/tasks"]
    argv += ["--timeout", timeout]
    for expectation in expectations:
        argv += ["--expect-task", expectation]
    for field in json_fields:
        argv += ["--expect-json", field]
    monkeypatch.setattr("sys.argv", argv)
    try:
        wait_for_http.main()
    except SystemExit as exc:
        return str(exc.code)
    return None


def _expire_after_first_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 100.0])
    monkeypatch.setattr(wait_for_http.time, "monotonic", lambda: next(clock))


def test_main_with_expected_tasks_present_returns_after_one_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _serve(
        monkeypatch,
        [_tasks(("e2e.add", "SUCCESS"), ("e2e.always_fails", "FAILURE"))],
    )

    result = _run_expecting(monkeypatch, "e2e.add=SUCCESS", "e2e.always_fails=FAILURE")

    assert result is None
    assert len(calls) == 1


def test_main_when_task_arrives_late_keeps_polling_until_it_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _serve(
        monkeypatch,
        [
            _tasks(),
            _tasks(("e2e.add", "STARTED")),
            _tasks(("e2e.add", "SUCCESS")),
        ],
    )

    result = _run_expecting(monkeypatch, "e2e.add=SUCCESS")

    assert result is None
    assert len(calls) == 3


def test_main_with_count_waits_for_enough_matching_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _serve(
        monkeypatch,
        [
            _tasks(("e2e.add", "SUCCESS")),
            _tasks(("e2e.add", "SUCCESS"), ("e2e.add", "SUCCESS")),
        ],
    )

    result = _run_expecting(monkeypatch, "e2e.add=SUCCESS:2")

    assert result is None
    assert len(calls) == 2


def test_main_matches_task_name_exactly_not_by_substring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, [_tasks(("e2e.add_many", "SUCCESS"))])
    _expire_after_first_poll(monkeypatch)

    result = _run_expecting(monkeypatch, "e2e.add=SUCCESS", timeout="10")

    assert result is not None
    assert "e2e.add=SUCCESS (0/1)" in result


def test_main_when_tasks_never_arrive_reports_what_it_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(
        monkeypatch,
        [_tasks(("e2e.add", "SUCCESS"), ("e2e.always_fails", "STARTED"))],
    )
    _expire_after_first_poll(monkeypatch)

    result = _run_expecting(
        monkeypatch,
        "e2e.add=SUCCESS:2",
        "e2e.always_fails=FAILURE",
        "e2e.aggregate=SUCCESS",
        timeout="10",
    )

    assert result == (
        "Service not healthy within 10.0s: waiting for tasks: "
        "e2e.add=SUCCESS (1/2), "
        "e2e.always_fails=FAILURE (0/1), "
        "e2e.aggregate=SUCCESS (0/1); "
        "seen: e2e.add=SUCCESS x1, e2e.always_fails=STARTED x1"
    )


def test_main_when_no_tasks_exist_reports_none_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, [_tasks()])
    _expire_after_first_poll(monkeypatch)

    result = _run_expecting(monkeypatch, "e2e.add=SUCCESS", timeout="10")

    assert result == (
        "Service not healthy within 10.0s: waiting for tasks: "
        "e2e.add=SUCCESS (0/1); seen: none"
    )


def test_main_when_body_has_unexpected_shape_keeps_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _serve(
        monkeypatch,
        [{"unexpected": "shape"}, _tasks(("e2e.add", "SUCCESS"))],
    )

    result = _run_expecting(monkeypatch, "e2e.add=SUCCESS")

    assert result is None
    assert len(calls) == 2


@pytest.mark.parametrize(
    "spec",
    ["e2e.add", "e2e.add=", "=SUCCESS", "e2e.add=SUCCESS:0", "e2e.add=SUCCESS:x"],
)
def test_main_with_malformed_expectation_exits_with_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], spec: str
) -> None:
    result = _run_expecting(monkeypatch, spec)

    assert result == "2"
    assert (
        f"{spec!r} is not NAME=STATE[:COUNT] with COUNT >= 1" in capsys.readouterr().err
    )


def test_main_with_unknown_task_state_exits_with_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _run_expecting(monkeypatch, "e2e.add=SUCCES")

    assert result == "2"
    assert "unknown state 'SUCCES'; expected one of PENDING, RECEIVED" in (
        capsys.readouterr().err
    )


def _health(**fields: object) -> dict[str, object]:
    return {"status": "ok", "consumer_connected": True, **fields}


def test_main_with_json_expectations_met_returns_after_one_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _serve(monkeypatch, [_health()])

    result = _run_expecting(
        monkeypatch, json_fields=("consumer_connected=true", "status=ok")
    )

    assert result is None
    assert len(calls) == 1


def test_main_when_consumer_connects_late_keeps_polling_until_it_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _serve(
        monkeypatch,
        [
            _health(status="degraded", consumer_connected=None),
            _health(status="degraded", consumer_connected=False),
            _health(),
        ],
    )

    result = _run_expecting(
        monkeypatch, json_fields=("consumer_connected=true", "status=ok")
    )

    assert result is None
    assert len(calls) == 3


def test_main_when_json_field_differs_reports_got_and_missing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, [{"status": "degraded", "consumer_connected": False}])
    _expire_after_first_poll(monkeypatch)

    result = _run_expecting(
        monkeypatch,
        timeout="10",
        json_fields=("status=ok", "consumer_connected=true", "version=1"),
    )

    assert result == (
        "Service not healthy within 10.0s: waiting for fields: "
        'status=ok (got "degraded"), '
        "consumer_connected=true (got false), "
        "version=1 (missing)"
    )


def test_main_json_expectation_does_not_treat_true_as_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, [{"count": True}])
    _expire_after_first_poll(monkeypatch)

    result = _run_expecting(monkeypatch, timeout="10", json_fields=("count=1",))

    assert result == (
        "Service not healthy within 10.0s: waiting for fields: count=1 (got true)"
    )


def test_main_with_json_and_task_expectations_reports_both_unmet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, [{**_tasks(), "status": "degraded"}])
    _expire_after_first_poll(monkeypatch)

    result = _run_expecting(
        monkeypatch,
        "e2e.add=SUCCESS",
        timeout="10",
        json_fields=("status=ok",),
    )

    assert result == (
        "Service not healthy within 10.0s: "
        'waiting for fields: status=ok (got "degraded"); '
        "waiting for tasks: e2e.add=SUCCESS (0/1); seen: none"
    )


@pytest.mark.parametrize("spec", ["status", "status=", "=ok"])
def test_main_with_malformed_json_expectation_exits_with_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], spec: str
) -> None:
    result = _run_expecting(monkeypatch, json_fields=(spec,))

    assert result == "2"
    assert f"{spec!r} is not KEY=VALUE" in capsys.readouterr().err


class _RawResponse(_FakeResponse):
    def __init__(self, body: bytes | Exception) -> None:
        self._body = body

    def read(self, size: int = -1) -> bytes:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _serve_raw(
    monkeypatch: pytest.MonkeyPatch, bodies: list[bytes | Exception]
) -> None:
    """Answer successive polls with raw bodies (the last one repeats)."""
    calls: list[int] = []

    def fake_open(request: urllib.request.Request, timeout: float) -> _RawResponse:
        calls.append(1)
        return _RawResponse(bodies[min(len(calls) - 1, len(bodies) - 1)])

    monkeypatch.setattr(wait_for_http._OPENER, "open", fake_open)
    monkeypatch.setattr(wait_for_http.time, "sleep", lambda _seconds: None)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"<html>502 Bad Gateway</html>", "response is not valid JSON ("),
        (b'{"tasks": [{"name": "e2e.add"', "response is not valid JSON ("),
        (b"\xff\xfe", "response is not valid JSON ("),
        (b"[1, 2]", "response is not a JSON object (got list)"),
    ],
)
def test_main_when_body_is_not_a_json_object_reports_it_on_timeout(
    monkeypatch: pytest.MonkeyPatch, body: bytes, expected: str
) -> None:
    _serve_raw(monkeypatch, [body])
    _expire_after_first_poll(monkeypatch)

    result = _run_expecting(monkeypatch, timeout="10", json_fields=("status=ok",))

    assert result is not None
    assert result.startswith(f"Service not healthy within 10.0s: {expected}")


def test_main_when_body_is_truncated_mid_read_retries_until_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    truncated = http.client.IncompleteRead(b'{"status": "o', 20)
    _serve_raw(monkeypatch, [truncated, json.dumps(_health()).encode()])

    result = _run_expecting(monkeypatch, json_fields=("status=ok",))

    assert result is None


def test_main_when_body_is_truncated_on_timeout_reports_the_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_raw(monkeypatch, [http.client.IncompleteRead(b'{"status": "o', 20)])
    _expire_after_first_poll(monkeypatch)

    result = _run_expecting(monkeypatch, timeout="10", json_fields=("status=ok",))

    assert result == (
        "Service not healthy within 10.0s: "
        "IncompleteRead(13 bytes read, 20 more expected)"
    )


def test_main_while_waiting_prints_progress_about_every_ten_seconds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(monkeypatch, [_tasks()])
    # start, then the time after each poll's sleep: polls happen at 0, 4, 11, 12
    # and 25. Progress is due at 11 (first 10s) and 25 (14s after that).
    clock = iter([0.0, 4.0, 11.0, 12.0, 25.0, 40.0])
    monkeypatch.setattr(wait_for_http.time, "monotonic", lambda: next(clock))

    result = _run_expecting(monkeypatch, "e2e.add=SUCCESS", timeout="30")

    assert result is not None
    assert capsys.readouterr().out.splitlines() == [
        "still waiting after 11s of 30s: waiting for tasks: "
        "e2e.add=SUCCESS (0/1); seen: none",
        "still waiting after 25s of 30s: waiting for tasks: "
        "e2e.add=SUCCESS (0/1); seen: none",
    ]


def test_task_states_match_the_stemtrace_task_state_enum() -> None:
    assert tuple(state.value for state in TaskState) == wait_for_http.TASK_STATES
