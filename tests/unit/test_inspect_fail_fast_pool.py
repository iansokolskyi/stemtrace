"""``_FailFastPool`` sets ``max_retries=0`` on ``ensure`` unless the caller passes one."""

from typing import Any

import pytest
from kombu import Connection

from stemtrace.server.api import routes


def _ensure(monkeypatch: pytest.MonkeyPatch, *args: Any, **kwargs: Any) -> Any:
    def record(self: Connection, obj: Any, fun: Any, *a: Any, **kw: Any) -> Any:
        return a, kw

    monkeypatch.setattr(Connection, "ensure", record)
    pool = routes._FailFastPool(Connection("memory://localhost"), 1)
    return pool.new().ensure(object(), len, *args, **kwargs)


def test_ensure_without_retry_arguments_gets_max_retries_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _ensure(monkeypatch) == ((), {"max_retries": 0})


def test_ensure_keeps_a_max_retries_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _ensure(monkeypatch, max_retries=3) == ((), {"max_retries": 3})


@pytest.mark.parametrize("positional", [(None, 5), (None, None)])
def test_ensure_keeps_a_positional_max_retries(
    monkeypatch: pytest.MonkeyPatch, positional: tuple[Any, ...]
) -> None:
    assert _ensure(monkeypatch, *positional) == (positional, {})
