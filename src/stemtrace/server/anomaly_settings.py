"""Build and validate the stuck-detection settings from arguments and env vars."""

import math
import os
from datetime import timedelta

from stemtrace.core.anomalies import DEFAULT_SETTINGS, AnomalySettings
from stemtrace.core.exceptions import ConfigurationError
from stemtrace.server.stats import MAX_DURATION, parse_duration

__all__ = [
    "CEILING_ENV_VAR",
    "FACTOR_ENV_VAR",
    "FLOOR_ENV_VAR",
    "MIN_SAMPLES_ENV_VAR",
    "anomaly_settings",
    "configured_anomaly_settings",
]

FACTOR_ENV_VAR = "STEMTRACE_ANOMALY_STUCK_FACTOR"
FLOOR_ENV_VAR = "STEMTRACE_ANOMALY_STUCK_FLOOR"
CEILING_ENV_VAR = "STEMTRACE_ANOMALY_STUCK_CEILING"
MIN_SAMPLES_ENV_VAR = "STEMTRACE_ANOMALY_STUCK_MIN_SAMPLES"

_PARAMETER_LABELS = (
    "anomaly_stuck_factor",
    "anomaly_stuck_floor",
    "anomaly_stuck_ceiling",
    "anomaly_stuck_min_samples",
)
_DURATION_HELP = (
    f"a positive duration like 90s, 10m, 1h or 7d (at most {MAX_DURATION.days}d)"
)


def anomaly_settings(
    *,
    stuck_factor: float | str | None = None,
    stuck_floor: timedelta | str | None = None,
    stuck_ceiling: timedelta | str | None = None,
    stuck_min_samples: int | str | None = None,
) -> AnomalySettings:
    """Validate the stuck settings; None keeps the default. Reads no env vars.

    Durations are a ``timedelta`` or a string like ``90s``, ``10m``, ``1h``
    or ``7d``.

    Raises:
        ConfigurationError: If a value isn't positive (``stuck_min_samples``
            at least 1), or the floor is above the ceiling.
    """
    return _build(
        _Input(stuck_factor, _PARAMETER_LABELS[0]),
        _Input(stuck_floor, _PARAMETER_LABELS[1]),
        _Input(stuck_ceiling, _PARAMETER_LABELS[2]),
        _Input(stuck_min_samples, _PARAMETER_LABELS[3]),
    )


def configured_anomaly_settings(
    *,
    stuck_factor: float | str | None = None,
    stuck_floor: timedelta | str | None = None,
    stuck_ceiling: timedelta | str | None = None,
    stuck_min_samples: int | str | None = None,
    argument_labels: tuple[str, str, str, str] | None = None,
) -> AnomalySettings:
    """As ``anomaly_settings``, but a missing or blank argument reads its env var.

    The variables are ``STEMTRACE_ANOMALY_STUCK_FACTOR``, ``_FLOOR``,
    ``_CEILING`` and ``_MIN_SAMPLES``; a blank one counts as unset.
    ``argument_labels`` renames the four arguments in error messages (the CLI
    passes its flags).

    Raises:
        ConfigurationError: As ``anomaly_settings``; the message names the
            env var when the value came from one.
    """
    factor, floor, ceiling, samples = argument_labels or _PARAMETER_LABELS
    return _build(
        _from_env(stuck_factor, factor, FACTOR_ENV_VAR),
        _from_env(stuck_floor, floor, FLOOR_ENV_VAR),
        _from_env(stuck_ceiling, ceiling, CEILING_ENV_VAR),
        _from_env(stuck_min_samples, samples, MIN_SAMPLES_ENV_VAR),
    )


class _Input:
    """A raw setting and the name an error message calls it."""

    def __init__(self, value: object, label: str) -> None:
        self.value = value
        self.label = label


def _from_env(argument: object, parameter: str, env_var: str) -> _Input:
    if not _is_blank(argument):
        return _Input(argument, parameter)
    from_env = os.getenv(env_var)
    return _Input(None if _is_blank(from_env) else from_env, env_var)


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _build(
    factor: _Input, floor: _Input, ceiling: _Input, min_samples: _Input
) -> AnomalySettings:
    defaults = DEFAULT_SETTINGS
    settings = AnomalySettings(
        stuck_factor=defaults.stuck_factor if factor.value is None else _factor(factor),
        stuck_floor=defaults.stuck_floor if floor.value is None else _duration(floor),
        stuck_ceiling=defaults.stuck_ceiling
        if ceiling.value is None
        else _duration(ceiling),
        stuck_min_samples=defaults.stuck_min_samples
        if min_samples.value is None
        else _min_samples(min_samples),
    )
    if settings.stuck_floor > settings.stuck_ceiling:
        raise ConfigurationError(
            f"The stuck floor ({int(settings.stuck_floor.total_seconds())}s) must not "
            f"exceed the stuck ceiling ({int(settings.stuck_ceiling.total_seconds())}s): "
            f"change {floor.label} or {ceiling.label}."
        )
    return settings


def _factor(setting: _Input) -> float:
    value = setting.value
    error = ConfigurationError(
        f"{setting.label} must be a positive number, got {value!r}."
    )
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise error
    try:
        number = float(value)
    except ValueError:
        raise error from None
    if not math.isfinite(number) or number <= 0:
        raise error
    return number


def _duration(setting: _Input) -> timedelta:
    value = setting.value
    if isinstance(value, timedelta):
        if timedelta(0) < value <= MAX_DURATION:
            return value
    elif isinstance(value, str):
        try:
            return parse_duration(value.strip())
        except ValueError:
            pass
    raise ConfigurationError(
        f"{setting.label} must be {_DURATION_HELP}, got {value!r}."
    )


def _min_samples(setting: _Input) -> int:
    value = setting.value
    error = ConfigurationError(
        f"{setting.label} must be a whole number of at least 1, got {value!r}."
    )
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise error
    try:
        number = int(value)
    except ValueError:
        raise error from None
    if number < 1:
        raise error
    return number
