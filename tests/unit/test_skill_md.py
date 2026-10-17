"""SKILL.md only uses endpoints, parameters and fields the API really has."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel

from stemtrace.core.events import TaskState
from stemtrace.server.api import schemas
from stemtrace.server.api.routes import create_api_router
from stemtrace.server.api.schemas import TaskStatus
from stemtrace.server.store import GraphStore

_SKILL = Path(__file__).resolve().parents[2] / "skills" / "stemtrace-api" / "SKILL.md"

# jq builtins and variables that look like fields but aren't
_NOT_FIELDS = frozenset({"0"})


def _skill_text() -> str:
    # Fail, don't skip, if the file moved.
    return _SKILL.read_text(encoding="utf-8")


def _bash_blocks() -> str:
    return "\n".join(re.findall(r"```bash\n(.*?)```", _skill_text(), flags=re.DOTALL))


def _routes() -> list[APIRoute]:
    router = create_api_router(GraphStore())
    return [r for r in router.routes if isinstance(r, APIRoute)]


def _route_regex(route: APIRoute) -> re.Pattern[str]:
    pattern = re.sub(r"\{[^}]+\}", r"[^/]+", route.path.removeprefix("/api/"))
    return re.compile(f"^{pattern}$")


def _used_paths() -> set[str]:
    """API paths the examples call: ``st PATH`` and ``st "PATH"`` (vars as ``X``)."""
    text = _skill_text()
    paths = set(re.findall(r'\bst "?([a-z][a-z/_$A-Z]*)"?(?=[\s|`])', text))
    return {re.sub(r"\$[A-Z_]+", "X", p) for p in paths}


def _query_keys() -> set[str]:
    text = _skill_text()
    return set(re.findall(r'--data-urlencode "([a-z_]+)=', text)) | set(
        re.findall(r"`([a-z_]+)=[^`]*`", text)
    )


def _jq_programs() -> list[str]:
    """Every jq program, in code blocks and inline code alike."""
    options = r'(?:\s+(?:-[a-z]+|--arg \w+ "[^"]*"|\\\n))*'
    return re.findall(rf"\bjq{options}\s*'([^']*)'", _skill_text())


def _schema_fields() -> set[str]:
    fields: set[str] = set()
    for _, model in inspect.getmembers(schemas, inspect.isclass):
        if issubclass(model, BaseModel) and model is not BaseModel:
            fields |= set(model.model_fields)
    return fields


def test_skill_examples_use_existing_endpoints() -> None:
    patterns = [_route_regex(r) for r in _routes()]
    used = _used_paths()

    assert {"health", "tasks", "tasks/X", "graphs/X", "graphs", "workers"} <= used
    unknown = {p for p in used if not any(rx.match(p) for rx in patterns)}
    assert unknown == set()


def test_skill_prose_names_only_existing_endpoints() -> None:
    patterns = [_route_regex(r) for r in _routes()]
    mentioned = {
        re.sub(r"\{[^}]+\}", "X", p).rstrip("/.,;:")
        # Up to whitespace, a quote, a backtick or a query string, so a
        # hyphenated or capitalized name (``tasks-recent``) is checked whole.
        for p in re.findall(r"/api/([^\s`'\"?)]+)", _skill_text())
        # Placeholders in the helper's own description: $path, PATH.
        if not p.startswith("$") and not p.isupper()
    }

    assert {"health", "health/live", "graphs/X"} <= mentioned
    assert {p for p in mentioned if not any(rx.match(p) for rx in patterns)} == set()


def test_skill_query_parameters_exist() -> None:
    accepted = {
        param.alias or param.name
        for route in _routes()
        for param in route.dependant.query_params
    }
    used = _query_keys()

    assert {"state", "from_date", "limit", "name", "status"} <= used
    assert used <= accepted
    # Named in the pitfalls prose rather than as key=value.
    for prose_param in ("name_exact", "to_date", "offset"):
        assert f"`{prose_param}`" in _skill_text()
        assert prose_param in accepted


def test_skill_jq_fields_exist_in_responses() -> None:
    programs = _jq_programs()
    assert len(programs) >= 8
    dotted = {f for p in programs for f in re.findall(r"\.([a-z_]+)", p)}
    shorthand = {
        f.strip()
        for p in programs
        for group in re.findall(r"\{([a-z_, ]+)\}", p)
        for f in group.split(",")
    }

    unknown = (dotted | shorthand) - _schema_fields() - _NOT_FIELDS
    assert unknown == set()
    assert {"exception", "traceback", "root_id", "node_type"} <= dotted | shorthand


def test_skill_lists_exactly_the_task_states() -> None:
    line = next(
        line for line in _skill_text().splitlines() if "States are uppercase" in line
    )
    listed = set(re.findall(r"`([A-Z ]+)`", line)[0].split())

    assert listed == {s.value for s in TaskState}


def test_skill_lists_exactly_the_registry_statuses() -> None:
    line = next(
        line for line in _skill_text().splitlines() if "Registry `status`" in line
    )

    assert set(re.findall(r"`([a-z_]+)`", line)) - {"status"} == {
        s.value for s in TaskStatus
    }


@pytest.mark.parametrize(
    ("path", "text"), [("/api/tasks", "tasks: 1 to"), ("/api/graphs", "graphs: 1 to")]
)
def test_skill_limits_match_the_api(path: str, text: str) -> None:
    route = next(r for r in _routes() if r.path == path)
    limit = next(p for p in route.dependant.query_params if p.name == "limit")
    upper = next(m.le for m in limit.field_info.metadata if hasattr(m, "le"))

    assert f"{text} {upper}, default {limit.field_info.default}" in _skill_text()
