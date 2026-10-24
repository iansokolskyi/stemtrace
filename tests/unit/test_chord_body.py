"""Tests for reading a chord body's first task ID (``core/chord_body.py``)."""

import json
from typing import Any

import pytest
from celery import Celery, chain, chord, group
from celery.canvas import Signature

from stemtrace.core.chord_body import body_first_task_id
from stemtrace.library.signals import _extract_chord_info

app = Celery("chord_body_tests", backend="cache+memory://")


def _sig(name: str) -> Signature:
    return app.signature(name)


def _frozen(body: Signature, task_id: str = "body-id") -> Signature:
    body.freeze(task_id)
    return body


def _as_dict(sig: Signature) -> Any:
    """The body as a worker deserializes it: plain JSON."""
    return json.loads(json.dumps(sig))


def _bodies() -> dict[str, tuple[Signature, str | None]]:
    first = _sig("t.first")
    first.freeze("first-id")
    return {
        "task": (_frozen(_sig("t.body")), "body-id"),
        "chain_task": (_frozen(chain(first.clone(), _sig("t.next"))), "first-id"),
        "chain_task_task": (
            _frozen(chain(first.clone(), _sig("t.b"), _sig("t.c"))),
            "first-id",
        ),
        "chain_chord": (
            _frozen(chain(chord([_sig("t.h")], _sig("t.i")), _sig("t.j"))),
            None,
        ),
        "chain_group": (
            _frozen(chain(group(_sig("t.g1"), _sig("t.g2")), _sig("t.j"))),
            None,
        ),
        "chord": (_frozen(chord([_sig("t.h1"), _sig("t.h2")], _sig("t.i"))), None),
        "group": (_frozen(group(_sig("t.g1"), _sig("t.g2"))), None),
    }


@pytest.mark.parametrize("kind", list(_bodies()))
def test_body_first_task_id_reads_each_body_kind(kind: str) -> None:
    body, expected = _bodies()[kind]
    assert body_first_task_id(body) == expected
    assert body_first_task_id(_as_dict(body)) == expected


def test_body_first_task_id_chain_keeps_first_step_not_last() -> None:
    body = chain(_sig("t.a"), _sig("t.b"), _sig("t.c"))
    body.freeze("last-id")
    steps = body.kwargs["tasks"]

    found = body_first_task_id(_as_dict(body))

    assert found == steps[0].options["task_id"]
    assert found != body.options["task_id"]


@pytest.mark.parametrize(
    "value",
    [
        None,
        "not a signature",
        42,
        [],
        {},
        {"task": "t.a"},
        {"task": "t.a", "options": None},
        {"task": "t.a", "options": {"task_id": 7}},
        {"task": "celery.chain"},
        {"task": "celery.chain", "kwargs": None},
        {"task": "celery.chain", "kwargs": {"tasks": []}},
        {"task": "celery.chain", "kwargs": {"tasks": "x"}},
        {"task": "celery.chain", "kwargs": {"tasks": [None]}},
        {"task": "celery.chain", "kwargs": {"tasks": [{"task": "celery.group"}]}},
    ],
)
def test_body_first_task_id_malformed_input_returns_none(value: object) -> None:
    assert body_first_task_id(value) is None


def test_body_first_task_id_deeply_nested_chain_stops_at_the_depth_bound() -> None:
    leaf: dict[str, Any] = {"task": "t.a", "options": {"task_id": "deep-id"}}
    nested = leaf
    for _ in range(20):
        nested = {"task": "celery.chain", "kwargs": {"tasks": [nested]}}

    assert body_first_task_id(nested) is None


@pytest.mark.parametrize("kind", list(_bodies()))
def test_extract_chord_info_returns_body_first_id_of_each_body_kind(kind: str) -> None:
    body, expected = _bodies()[kind]

    _, callback_id, first_id = _extract_chord_info(_as_dict(body))

    assert callback_id == body.options["task_id"]
    assert first_id == expected


def test_extract_chord_info_without_a_chord_returns_no_body_first_id() -> None:
    assert _extract_chord_info(None) == (None, None, None)
