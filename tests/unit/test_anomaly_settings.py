"""The four stuck settings: kwargs, env vars, CLI flags, validation and effect."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import stemtrace
from stemtrace.core.anomalies import (
    DEFAULT_SETTINGS,
    MAX_STUCK_FACTOR,
    AnomalySettings,
)
from stemtrace.core.events import TaskEvent, TaskState
from stemtrace.core.exceptions import ConfigurationError
from stemtrace.server import __main__ as cli
from stemtrace.server.anomaly_settings import (
    CEILING_ENV_VAR,
    FACTOR_ENV_VAR,
    FLOOR_ENV_VAR,
    MIN_SAMPLES_ENV_VAR,
    anomaly_settings,
    configured_anomaly_settings,
)
from stemtrace.server.api import params, routes
from stemtrace.server.fastapi import extension as extension_module
from stemtrace.server.fastapi.extension import StemtraceExtension
from stemtrace.server.ui import static
from tests.unit.anomaly_pipeline import at, completed_runs, task_event

ALL_ENV = [FACTOR_ENV_VAR, FLOOR_ENV_VAR, CEILING_ENV_VAR, MIN_SAMPLES_ENV_VAR]
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class Flip:
    """A scenario that is not stuck by default and is stuck with one setting changed."""

    events: list[TaskEvent]
    now_s: float
    kwarg: str
    value: str
    env_var: str
    flag: str


def _one_running(seconds: float = 0) -> TaskEvent:
    return task_event("run", TaskState.STARTED, seconds, name="app.work", host="w@a")


FLIPS = {
    # n = 0: T = max(ceiling, floor). 30 min running: under 1 h, over 20 min.
    "ceiling": Flip(
        [_one_running()],
        30 * 60,
        "anomaly_stuck_ceiling",
        "20m",
        CEILING_ENV_VAR,
        "--anomaly-stuck-ceiling",
    ),
    # n = 5, p95 = 1 s: T = max(3 s, floor). 5 min running: under 10 min, over 1 min.
    "floor": Flip(
        [*completed_runs("app.work", 5, 1), _one_running()],
        5 * 60,
        "anomaly_stuck_floor",
        "1m",
        FLOOR_ENV_VAR,
        "--anomaly-stuck-floor",
    ),
    # n = 5, p95 = 10 min: T = max(k x 10 min, 10 min). 20 min: under 30 min, over 10 min.
    "factor": Flip(
        [*completed_runs("app.work", 5, 600), _one_running()],
        20 * 60,
        "anomaly_stuck_factor",
        "1",
        FACTOR_ENV_VAR,
        "--anomaly-stuck-factor",
    ),
    # n = 3, p95 = 1 s: N = 5 falls back to the 1 h ceiling, N = 3 uses the 10 min floor.
    "min_samples": Flip(
        [*completed_runs("app.work", 3, 1), _one_running()],
        30 * 60,
        "anomaly_stuck_min_samples",
        "3",
        MIN_SAMPLES_ENV_VAR,
        "--anomaly-stuck-min-samples",
    ),
}


def _typed(kwarg: str, value: str) -> Any:
    """The kwarg as a Python caller writes it: a timedelta, float or int."""
    if kwarg.endswith(("floor", "ceiling")):
        return timedelta(minutes=int(value.rstrip("m")))
    return float(value) if kwarg.endswith("factor") else int(value)


def _stuck_ids(
    extension: StemtraceExtension, flip: Flip, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    for event in flip.events:
        extension.store.add_event(event)
    now = at(flip.now_s)
    monkeypatch.setattr(params, "utcnow", lambda: now)
    app = FastAPI()
    app.include_router(extension.router)
    body = TestClient(app).get("/api/anomalies?kind=stuck").json()
    return [item["task_id"] for item in body["anomalies"]]


def _extension(**kwargs: Any) -> StemtraceExtension:
    return StemtraceExtension(
        broker_url="memory://", embedded_consumer=False, serve_ui=False, **kwargs
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ALL_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_inspect(monkeypatch: pytest.MonkeyPatch) -> None:
    """The extension has a broker URL, so /anomalies would ask real workers."""

    async def skip(_self: object) -> None:
        return None

    monkeypatch.setattr(routes._InspectRefresher, "maybe_refresh", skip)


# --- the builder -----------------------------------------------------------


def test_anomaly_settings_without_input_is_the_documented_default() -> None:
    assert anomaly_settings() == DEFAULT_SETTINGS
    assert (
        AnomalySettings(3, timedelta(minutes=10), timedelta(hours=1), 5)
        == DEFAULT_SETTINGS
    )


def test_anomaly_settings_accepts_numbers_timedeltas_and_duration_strings() -> None:
    assert anomaly_settings(
        stuck_factor=2.5,
        stuck_floor=timedelta(seconds=90),
        stuck_ceiling="2h",
        stuck_min_samples=7,
    ) == AnomalySettings(2.5, timedelta(seconds=90), timedelta(hours=2), 7)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "stuck_factor",
            0,
            "anomaly_stuck_factor must be a positive number of at most 100000, got 0.",
        ),
        (
            "stuck_factor",
            -1,
            "anomaly_stuck_factor must be a positive number of at most 100000, got -1.",
        ),
        ("stuck_factor", "abc", "of at most 100000, got 'abc'."),
        ("stuck_factor", float("inf"), "must be a positive number"),
        ("stuck_factor", float("nan"), "must be a positive number"),
        ("stuck_factor", True, "of at most 100000, got True."),
        ("stuck_factor", 100_001, "of at most 100000, got 100001."),
        ("stuck_factor", "1e15", "of at most 100000, got '1e15'."),
        ("stuck_factor", 1e308, "must be a positive number of at most 100000"),
        (
            "stuck_floor",
            timedelta(0),
            "anomaly_stuck_floor must be a positive duration",
        ),
        ("stuck_floor", timedelta(seconds=-5), "must be a positive duration"),
        ("stuck_floor", "10", "must be a positive duration like 90s, 10m, 1h or 7d"),
        ("stuck_floor", "0m", "must be a positive duration"),
        ("stuck_floor", "-1h", "must be a positive duration"),
        ("stuck_floor", 600, "must be a positive duration"),
        ("stuck_ceiling", "4000d", "at most 3650d"),
        ("stuck_ceiling", timedelta(days=4000), "at most 3650d"),
        ("stuck_min_samples", 0, "anomaly_stuck_min_samples must be a whole number"),
        ("stuck_min_samples", -3, "must be a whole number of at least 1, got -3."),
        ("stuck_min_samples", "2.5", "must be a whole number of at least 1"),
        ("stuck_min_samples", True, "must be a whole number"),
        ("stuck_min_samples", 2.0, "must be a whole number"),
    ],
)
def test_anomaly_settings_rejects_bad_values_with_a_clear_message(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=re.escape(message)):
        anomaly_settings(**{field: value})


def test_anomaly_settings_accepts_the_largest_factor() -> None:
    assert anomaly_settings(stuck_factor=MAX_STUCK_FACTOR).stuck_factor == 100_000


def test_anomaly_settings_accepts_a_floor_above_the_ceiling() -> None:
    settings = anomaly_settings(stuck_floor="2h")

    assert settings.stuck_floor == timedelta(hours=2)
    assert settings.stuck_ceiling == timedelta(hours=1)


def test_anomaly_settings_accepts_a_floor_equal_to_the_ceiling() -> None:
    settings = anomaly_settings(stuck_floor="15m", stuck_ceiling="15m")

    assert settings.stuck_floor == settings.stuck_ceiling == timedelta(minutes=15)


# --- env vars --------------------------------------------------------------


def test_configured_settings_read_each_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(FACTOR_ENV_VAR, "2")
    monkeypatch.setenv(FLOOR_ENV_VAR, "90s")
    monkeypatch.setenv(CEILING_ENV_VAR, "2h")
    monkeypatch.setenv(MIN_SAMPLES_ENV_VAR, "8")

    assert configured_anomaly_settings() == AnomalySettings(
        2.0, timedelta(seconds=90), timedelta(hours=2), 8
    )


def test_configured_settings_prefer_the_argument_over_the_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CEILING_ENV_VAR, "2h")

    assert configured_anomaly_settings(stuck_ceiling="30m").stuck_ceiling == timedelta(
        minutes=30
    )


@pytest.mark.parametrize("blank", ["", "   "])
def test_configured_settings_treat_a_blank_env_var_as_unset(
    blank: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ALL_ENV:
        monkeypatch.setenv(name, blank)

    assert configured_anomaly_settings() == DEFAULT_SETTINGS


@pytest.mark.parametrize(
    ("env_var", "value", "message"),
    [
        (
            FACTOR_ENV_VAR,
            "x",
            f"{FACTOR_ENV_VAR} must be a positive number of at most 100000, got 'x'.",
        ),
        (FLOOR_ENV_VAR, "0", f"{FLOOR_ENV_VAR} must be a positive duration"),
        (CEILING_ENV_VAR, "-5m", f"{CEILING_ENV_VAR} must be a positive duration"),
        (MIN_SAMPLES_ENV_VAR, "0", f"{MIN_SAMPLES_ENV_VAR} must be a whole number"),
    ],
)
def test_configured_settings_name_the_env_var_in_the_error(
    env_var: str, value: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_var, value)

    with pytest.raises(ConfigurationError, match=re.escape(message)):
        configured_anomaly_settings()


# --- every setting changes what the API reports ----------------------------


@pytest.mark.parametrize("setting", FLIPS)
def test_default_settings_do_not_flag_the_scenario(
    setting: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _stuck_ids(_extension(), FLIPS[setting], monkeypatch) == []


@pytest.mark.parametrize("setting", FLIPS)
def test_extension_kwarg_changes_the_stuck_threshold(
    setting: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    flip = FLIPS[setting]
    extension = _extension(**{flip.kwarg: _typed(flip.kwarg, flip.value)})

    assert _stuck_ids(extension, flip, monkeypatch) == ["run"]


@pytest.mark.parametrize("setting", FLIPS)
def test_init_app_env_var_changes_the_stuck_threshold(
    setting: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    flip = FLIPS[setting]
    monkeypatch.setenv(flip.env_var, flip.value)
    extension = stemtrace.init_app(
        FastAPI(), broker_url="memory://", embedded_consumer=False, serve_ui=False
    )

    assert _stuck_ids(extension, flip, monkeypatch) == ["run"]


@pytest.mark.parametrize("setting", FLIPS)
def test_init_app_kwarg_changes_the_stuck_threshold(
    setting: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    flip = FLIPS[setting]
    extension = stemtrace.init_app(
        FastAPI(),
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        **{flip.kwarg: _typed(flip.kwarg, flip.value)},
    )

    assert _stuck_ids(extension, flip, monkeypatch) == ["run"]


def test_init_app_kwarg_wins_over_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    flip = FLIPS["ceiling"]
    monkeypatch.setenv(CEILING_ENV_VAR, "20m")
    extension = stemtrace.init_app(
        FastAPI(),
        broker_url="memory://",
        embedded_consumer=False,
        serve_ui=False,
        anomaly_stuck_ceiling="2h",
    )

    assert _stuck_ids(extension, flip, monkeypatch) == []


@pytest.mark.parametrize("setting", FLIPS)
def test_bad_extension_kwarg_raises_configuration_error(setting: str) -> None:
    flip = FLIPS[setting]
    bad = "-1" if setting in {"factor", "min_samples"} else "0m"

    with pytest.raises(ConfigurationError, match=flip.kwarg):
        _extension(**{flip.kwarg: bad})


def test_init_app_with_a_bad_env_var_raises_and_leaves_the_app_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(FACTOR_ENV_VAR, "fast")
    app = FastAPI()
    routes_before = list(app.routes)

    with pytest.raises(ConfigurationError, match=FACTOR_ENV_VAR):
        stemtrace.init_app(app, broker_url="memory://", embedded_consumer=False)

    assert list(app.routes) == routes_before


# --- the CLI ---------------------------------------------------------------


@pytest.fixture
def captured_extensions(monkeypatch: pytest.MonkeyPatch) -> list[StemtraceExtension]:
    """Record the extension the CLI builds; serve nothing."""
    captured: list[StemtraceExtension] = []

    class _Recording(StemtraceExtension):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]  # forwarding the CLI's kwargs as-is
            captured.append(self)

    monkeypatch.setattr(extension_module, "StemtraceExtension", _Recording)
    monkeypatch.setattr(
        static, "get_static_router_with_base", lambda *_a, **_k: APIRouter()
    )
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: None)
    monkeypatch.delenv("STEMTRACE_TRANSPORT_URL", raising=False)
    return captured


def _run(*args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    result = CliRunner().invoke(
        cli.app, ["server", "--broker-url", "memory://", *args], env=env
    )
    return result.exit_code, _ANSI.sub("", result.output)


@pytest.mark.parametrize("setting", FLIPS)
def test_cli_flag_changes_the_stuck_threshold(
    setting: str,
    captured_extensions: list[StemtraceExtension],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flip = FLIPS[setting]

    code, output = _run(flip.flag, flip.value)

    assert code == 0, output
    assert _stuck_ids(captured_extensions[0], flip, monkeypatch) == ["run"]


@pytest.mark.parametrize("setting", FLIPS)
def test_cli_env_var_changes_the_stuck_threshold(
    setting: str,
    captured_extensions: list[StemtraceExtension],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flip = FLIPS[setting]

    code, output = _run(env={flip.env_var: flip.value})

    assert code == 0, output
    assert _stuck_ids(captured_extensions[0], flip, monkeypatch) == ["run"]


def test_cli_flag_wins_over_the_env_var(
    captured_extensions: list[StemtraceExtension], monkeypatch: pytest.MonkeyPatch
) -> None:
    flip = FLIPS["ceiling"]

    code, output = _run(flip.flag, "2h", env={flip.env_var: "20m"})

    assert code == 0, output
    assert _stuck_ids(captured_extensions[0], flip, monkeypatch) == []


@pytest.mark.parametrize("setting", FLIPS)
def test_cli_without_flags_uses_the_defaults(
    setting: str,
    captured_extensions: list[StemtraceExtension],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, output = _run(env={FLIPS[setting].env_var: " "})

    assert code == 0, output
    assert _stuck_ids(captured_extensions[0], FLIPS[setting], monkeypatch) == []


@pytest.mark.parametrize("setting", FLIPS)
def test_cli_with_a_bad_value_is_a_usage_error_naming_the_flag(
    setting: str, captured_extensions: list[StemtraceExtension]
) -> None:
    flip = FLIPS[setting]

    code, output = _run(flip.flag, "-1")

    assert code == 2
    assert flip.flag in output
    assert captured_extensions == []


# --- a floor above the ceiling: T = max(ceiling, floor) -------------------------

_RUNNING = [task_event("run", TaskState.STARTED, 0, name="app.work", host="w@a")]


def _thresholds(
    extension: StemtraceExtension, minutes: int, monkeypatch: pytest.MonkeyPatch
) -> list[int | None]:
    """The ``threshold_ms`` of the stuck tasks ``minutes`` after the task started."""
    for event in _RUNNING:
        extension.store.add_event(event)
    app = FastAPI()
    app.include_router(extension.router)
    now = at(minutes * 60)
    monkeypatch.setattr(params, "utcnow", lambda: now)
    body = TestClient(app).get("/api/anomalies?kind=stuck").json()
    return [item["threshold_ms"] for item in body["anomalies"]]


@pytest.mark.parametrize(("minutes", "expected"), [(90, []), (150, [7_200_000])])
def test_floor_alone_above_the_default_ceiling_sets_the_threshold_to_the_floor(
    minutes: int, expected: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FLOOR_ENV_VAR, "2h")
    extension = stemtrace.init_app(
        FastAPI(), broker_url="memory://", embedded_consumer=False, serve_ui=False
    )

    # No finished runs, so T = max(ceiling 1 h, floor 2 h) = 2 h.
    assert _thresholds(extension, minutes, monkeypatch) == expected


def test_cli_with_a_floor_above_the_ceiling_starts_and_uses_the_floor(
    captured_extensions: list[StemtraceExtension], monkeypatch: pytest.MonkeyPatch
) -> None:
    code, output = _run("--anomaly-stuck-floor", "2h")

    assert code == 0, output
    assert _thresholds(captured_extensions[0], 150, monkeypatch) == [7_200_000]
